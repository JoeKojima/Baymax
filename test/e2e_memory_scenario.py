"""
End-to-end scenario for Ember's people-awareness and memory, on the real stack:
realtime_gemini_10 + the robot's SemanticEmbedder (all-MiniLM-L6-v2 ONNX) +
ChromaDB + the YuNet/SFace face models + real face photos. Only Gemini (Live
and Flash) is scripted, and the camera/mic/speaker are faked.

Run through test/test_memory_end_to_end.py (it needs EMBER_FACE_TEST_DATA and
Baymax-main/all-MiniLM-L6-v2-onnx). Prints PASS on success.
"""
import asyncio, importlib, json, os, sys, tempfile, threading, time
from unittest import mock
import cv2
HERE = os.path.dirname(os.path.abspath(__file__))
BAYMAX = os.path.join(HERE, "..", "Baymax-main")
sys.path.insert(0, BAYMAX); sys.path.insert(0, HERE)
for name in ["mediapipe", "mediapipe.tasks", "mediapipe.tasks.python", "mediapipe.tasks.python.vision"]:
    sys.modules[name] = mock.MagicMock(name=name)
sd = mock.MagicMock()
sd.query_devices.return_value = [{"name": "pipewire", "max_output_channels": 0, "max_input_channels": 2},
                                 {"name": "default", "max_output_channels": 2, "max_input_channels": 0}]
sys.modules["sounddevice"] = sd
os.environ.pop("BAYMAX_DEVICE_KEY", None)

import ember_self, face_id, person_memory, test_face_models as T
from semantic_embedder import SemanticEmbedder
tmp = tempfile.mkdtemp()
ember_self.PROFILE_PATH = os.path.join(tmp, "ember_profile.json")
face_id.PEOPLE_PATH = os.path.join(tmp, "ember_people.json")
person_memory.PENDING_PATH = os.path.join(tmp, "conversation_pending.jsonl")
person_memory.CONVERSATION_GAP = 2.0
face_id.GREET_COOLDOWN = 1.0
ember_self.save_profile({"user_name": "Margaret", "household": [
    {"name": "Sarah", "relationship": "daughter"}, {"name": "Tom Jones", "relationship": "son"}]})
with mock.patch("subprocess.run"), mock.patch("subprocess.check_output", return_value=b""):
    R = importlib.import_module("realtime_gemini_10")
from google.genai import types
R.TRANSCRIPT_DIR = tmp
R.MEMORY_CHECK_SECONDS = 0.5
R._self_state = ember_self.SelfState(debounce=0.5, min_interval=0.5, refresh_interval=3600)
MODEL_DIR = os.path.join(BAYMAX, "all-MiniLM-L6-v2-onnx")
R._memory_embedder = SemanticEmbedder(MODEL_DIR, verbose=False, chroma_dir=os.path.join(tmp, "chroma"),
                                      collection_name="user_memories")
db = R._memory_embedder
# A memory from before people were tracked (old format, no people)
db.save(["Margaret was a schoolteacher for thirty years."], ids=["memory_20260101_120000_0"],
        metadatas=[{"source": "conversation_summary", "timestamp": "20260101_120000", "line_index": "0"}])

flash = {"summary": [], "redact": [], "appearance": []}
SUMMARIES = []   # queue of replies for successive conversations
def fake_flash(prompt, image_jpeg=None, json_reply=True):
    if image_jpeg is not None:
        flash["appearance"].append(prompt)
        assert image_jpeg[:2] == b"\xff\xd8"
        if "Their look last time" in prompt:
            return json.dumps({"look": "Short brown hair", "today": "a blue scarf",
                               "changed": True, "change": "shorter hair"})
        return json.dumps({"look": "Long brown hair", "today": "a bright red cardigan",
                           "changed": False, "change": ""})
    if prompt.startswith("Rewrite this memory"):
        flash["redact"].append(prompt)
        assert not json_reply, "rewrites must ask for plain text"
        memory = prompt.split("Memory: ", 1)[1]
        return {"Sarah told Margaret about her job interview on Friday.":
                    "Margaret heard about a family member's job interview.",
                "On Wednesday, Sarah and Margaret talked with Ember about Sarah's job interview, Margaret's hip and Tom's surgery.":
                    "Margaret talked with Ember about her hip and Tom's surgery."}.get(memory, "DELETE")
    assert prompt.startswith("You are the memory of Ember")
    flash["summary"].append(prompt)
    return json.dumps({"memories": SUMMARIES.pop(0)})
R._flash = fake_flash

T.DATA = os.environ["EMBER_FACE_TEST_DATA"]
models = face_id.FaceModels(face_id.ensure_models())
people = []
for name, photos in T._people(12):
    solo = [p for p in photos if len(models.detect(cv2.imread(p))) == 1]
    if len(solo) >= 11: people.append(solo)
    if len(people) == 4: break
margaret, sarah, dave, stranger = people
store = face_id.PeopleStore()
for who, photos in (("Margaret", margaret), ("Sarah", sarah), ("Dave", dave)):
    embs = [e for e, _ in (face_id.embeddings_from_photo(models, open(p, "rb").read()) for p in photos[:5]) if e is not None]
    store.add_samples(who, embs, source="app")
R._init_face_id()

sent = []
class FakeSession:
    def __init__(self): self.q = asyncio.Queue()
    async def receive(self):
        while True:
            yield await self.q.get()
    async def send_client_content(self, *, turns, turn_complete):
        sent.append((turns["parts"][0]["text"], turn_complete))
    async def send_tool_response(self, *, function_responses):
        sent.append(("[TOOL RESPONSE] " + json.dumps([r.response for r in function_responses]), None))

scene = {"photos": [], "i": 0}
async def camera():
    while True:
        scene["i"] += 1
        k = scene["i"] // 8
        with R._latest_frame_lock:
            R._latest_frame = T.robot_frame([ph[k % len(ph)] for ph in scene["photos"]], face_px=90, seed=k)
        await asyncio.sleep(1 / 15)
def of(prefix): return [m for m, _ in sent if m.startswith(prefix)]
async def wait_for(pred, timeout=25, what=""):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if pred(): return
        await asyncio.sleep(0.1)
    raise AssertionError(f"timed out: {what}\nlast sent: {[m[:100] for m, _ in sent[-6:]]}")
def msg(**kw): return types.LiveServerMessage(server_content=types.LiveServerContent(**kw))
async def say(s, user=None, ember=None):
    if user: await s.q.put(msg(input_transcription=types.Transcription(text=user)))
    if ember:
        await s.q.put(msg(output_transcription=types.Transcription(text=ember)))
        await s.q.put(msg(turn_complete=True))
def memories():
    got = db._collection.get(include=["documents", "metadatas"])
    return {d: person_memory._decode_people(m.get("people")) for d, m in zip(got["documents"], got["metadatas"])}
def away(name, seconds):
    pid = face_id.PeopleStore().find_by_name(name)["id"]
    R._face_id._last_present[pid] = time.time() - seconds
    R._face_id.store.touch({pid: time.time() - seconds})
    data = json.load(open(face_id.PEOPLE_PATH))
    for p in data["people"]:
        if p["id"] == pid and p.get("appearance"): p["appearance"]["t"] = time.time() - seconds
    json.dump(data, open(face_id.PEOPLE_PATH, "w"))

async def main():
    # 0. Prompt at session start includes the old-format memory, dated
    si = R._build_config()["system_instruction"]
    assert "- Margaret was a schoolteacher for thirty years. (Jan 1)" in si, si[:300]
    assert "# Talking with different people" in si and "be warm but discreet" in si
    print("0 prompt memory block ok")

    s = FakeSession()
    threading.Thread(target=R._face_id_thread, daemon=True).start()
    threading.Thread(target=R._memory_thread, daemon=True).start()
    tasks = [asyncio.create_task(camera()), asyncio.create_task(R.self_awareness_monitor(s)),
             asyncio.create_task(R.receive_audio(s))]
    with R._initiation_ts_lock: R._last_loud_mic_ts = 0.0

    # 1. Wednesday: Margaret and Sarah together, a conversation
    scene["photos"] = [margaret[5:], sarah[5:]]
    await wait_for(lambda: len(of("[ARRIVED]")) == 2, what="both greeted")
    SUMMARIES.append([
        {"memory": "Sarah told Margaret about her job interview on Friday.", "people": ["Sarah", "Margaret"]},
        {"memory": "Margaret's hip has been hurting.", "people": ["Margaret"]},
        {"memory": "Margaret is worried about Tom's knee surgery next month.", "people": ["Margaret", "Tom"]},
        {"memory": "On Wednesday, Sarah and Margaret talked with Ember about Sarah's job interview, Margaret's hip and Tom's surgery.",
         "people": ["Sarah", "Margaret"], "kind": "conversation"}])
    await say(s, "Hi Ember, it's Sarah. I have a job interview on Friday!", "Good luck, Sarah!")
    await say(s, "My hip has been hurting, and Tom has knee surgery next month.", "I'm sorry, Margaret.")
    await wait_for(lambda: len(memories()) == 5, timeout=20, what="memories saved after the gap")
    prompt = flash["summary"][0]
    assert "Tom Jones (son)" in prompt and "Dave" in prompt and "EVERYONE it involves" in prompt
    assert ("in front of Ember: Margaret, Sarah" in prompt) or ("in front of Ember: Sarah, Margaret" in prompt)
    m = memories()
    assert m["Sarah told Margaret about her job interview on Friday."] == ["Sarah", "Margaret"]
    assert m["Margaret is worried about Tom's knee surgery next month."] == ["Margaret", "Tom Jones"]
    print("1 saved:", json.dumps(m, indent=1))

    # 2. Next morning Margaret alone: her greeting includes the shared memory + Tom's surgery
    scene["photos"] = []; await asyncio.sleep(3)
    away("Margaret", 10 * 3600)
    n = len(of("[ARRIVED]")); scene["photos"] = [margaret[1:5]]
    await wait_for(lambda: len(of("[ARRIVED]")) == n + 1, what="Margaret greeted")
    g = of("[ARRIVED]")[-1]
    print("2 Margaret's greeting:\n" + g)
    assert g.startswith("[ARRIVED] Margaret just came into view.")
    assert "Sarah told Margaret about her job interview on Friday. (today; with Sarah)" in g
    assert "Tom's knee surgery next month. (today; with Tom Jones)" in g
    assert "schoolteacher" in g     # her old-format memory too

    # 3. Friday: Sarah alone, 2 days later — shared memory, haircut, nothing about Tom
    scene["photos"] = []; await asyncio.sleep(3)
    away("Sarah", 2 * 86400)
    n = len(of("[ARRIVED]")); scene["photos"] = [sarah[1:5]]
    await wait_for(lambda: len(of("[ARRIVED]")) == n + 1, what="Sarah greeted")
    g = of("[ARRIVED]")[-1]
    print("3 Sarah's greeting:\n" + g)
    assert "You last saw Sarah 2 days ago" in g
    assert "Sarah told Margaret about her job interview on Friday. (today; with Margaret)" in g
    # memories that don't involve Sarah are not listed (the conversation summary she
    # was part of may mention them — she was there)
    assert "- Margaret's hip has been hurting." not in g
    assert "- Margaret is worried about Tom's knee surgery" not in g
    assert "schoolteacher" not in g
    assert "shorter hair" in g and "haircut" in g
    assert "Today Sarah has on: a blue scarf." in g
    await wait_for(lambda: "Sarah just arrived" in ([m for m, _ in sent if m.startswith("[SELF]")] or [""])[-1],
                   what="[SELF] says Sarah just arrived")

    # 4. Recall while talking: memories saved after this session's prompt are recalled
    #    and reach Gemini BEFORE its reply starts; nothing unrelated is recalled.
    before = len(of("[MEMORY CONTEXT]"))
    await s.q.put(msg(input_transcription=types.Transcription(text="The interview went really well!")))
    await wait_for(lambda: len(of("[MEMORY CONTEXT]")) == before + 1, timeout=10, what="recall")
    ctx = of("[MEMORY CONTEXT]")[-1]
    print("4 recalled:", ctx.replace("\n", " | "))
    assert "job interview on Friday" in ctx
    assert sent[-1][0].startswith("[MEMORY CONTEXT]") and sent[-1][1] is False   # before any reply
    await s.q.put(msg(output_transcription=types.Transcription(text="That's wonderful!")))
    await s.q.put(msg(turn_complete=True))
    await s.q.put(msg(input_transcription=types.Transcription(text="what time is it")))
    await asyncio.sleep(1.5)
    assert len(of("[MEMORY CONTEXT]")) == before + 1, "unrelated chit-chat recalled memories / repeated one"
    SUMMARIES.append([
        {"memory": "Sarah's job interview went really well.", "people": ["Sarah"]},
        {"memory": "Margaret's hip has been hurting again.", "people": ["Margaret"]}])
    await s.q.put(msg(output_transcription=types.Transcription(text="It's three o'clock.")))
    await s.q.put(msg(turn_complete=True))
    await wait_for(lambda: len(flash["summary"]) == 2 and not R._conversation.pending(), what="2nd save")
    m = memories()
    print("4 after 2nd conversation:", json.dumps(m, indent=1))
    assert "Margaret's hip has been hurting again." in m and "Margaret's hip has been hurting." not in m, \
        "repeat wasn't merged"
    assert len(m) == 6

    # 5. Opening a conversation: addressed to who's actually here (Sarah), with her
    #    look today and only memories that involve her
    await R._fire_initiation(s)
    init = of("[INITIATE CONVERSATION]")[-1]
    print("5 conversation starter:\n" + init)
    assert init.startswith("[INITIATE CONVERSATION] With you now: Sarah (Margaret's daughter, just arrived)."), init
    assert "start a conversation with Sarah" in init and "The user" not in init
    assert "today Sarah has on a blue scarf" in init
    hint = init.split("Things you remember", 1)[1]
    assert "- Sarah's job interview went really well." in hint, init
    assert "- Margaret is worried about Tom's knee surgery" not in hint, init
    assert "- Margaret's hip has been hurting again." not in hint, init
    print("5 initiation hint ok")

    # 6. Sarah: "please forget me" → face + memories (shared ones rewritten without her)
    await s.q.put(types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[
        types.FunctionCall(id="f1", name="forget_person", args={"name": "Sarah"})])))
    await wait_for(lambda: any(m.startswith("[TOOL RESPONSE]") for m, _ in sent), what="forget")
    resp = json.loads(of("[TOOL RESPONSE]")[-1][len("[TOOL RESPONSE] "):])[0]
    print("6 forget ->", resp)
    assert resp["status"] == "forgotten" and resp["memories_deleted"] == 1 and resp["memories_rewritten"] == 2
    m = memories()
    assert not any("Sarah" in d or "Sarah" in p for d, p in m.items()), m
    assert m["Margaret heard about a family member's job interview."] == ["Margaret"]
    assert face_id.PeopleStore().find_by_name("Sarah") is None
    assert person_memory.memories_involving(db, "Sarah", R._profile) == []

    # 7. Dashboard removal of Dave → the core forgets him in memories too
    db.save(["Dave fixed Margaret's fence."], ids=["dave1"], metadatas=[person_memory._metadata(["Dave", "Margaret"], time.time())])
    db.save(["Dave plays golf."], ids=["dave2"], metadatas=[person_memory._metadata(["Dave"], time.time())])
    face_id.PeopleStore().remove(face_id.PeopleStore().find_by_name("Dave")["id"], forget_memories=True)
    await wait_for(lambda: "Dave plays golf." not in memories() and "Dave fixed Margaret's fence." not in memories(),
                   timeout=10, what="dashboard forget")
    print("7 dashboard forget ok:", sorted(memories()))

    # 8. Shutdown saves an unfinished conversation
    SUMMARIES.append([{"memory": "Margaret wants to plant tulips in spring.", "people": ["Margaret"]}])
    R._conversation.add("user", "I want to plant tulips in spring", ["Margaret"])
    R._summarise_and_embed()
    assert "Margaret wants to plant tulips in spring." in memories() and not R._conversation.pending()
    print("8 shutdown save ok")

    # 9. Recall arriving mid-reply is held until the turn ends; nothing is repeated;
    #    memories already in the prompt aren't re-sent; unrelated requests recall nothing.
    class Quiet:
        def __init__(self): self.out = []
        async def send_client_content(self, *, turns, turn_complete):
            self.out.append(turns["parts"][0]["text"])
    async def recall(text, reply_started=False, known=()):
        q = Quiet(); inj = R._MemoryInjector(q, already_known=known)
        if reply_started: inj.reply_started()
        inj.user_spoke(text)
        await inj.task
        return q, inj
    q, inj = await recall("did Margaret plant the tulips", reply_started=True)
    assert q.out == [] and inj.held, (q.out, inj.held)              # held during the reply
    await inj.turn_ended()
    assert len(q.out) == 1 and "tulips" in q.out[0]                  # ...sent when it ends
    inj.user_spoke("the tulips in spring")
    await inj.task
    assert len(q.out) == 1, "same memory sent twice"
    q, _ = await recall("did Margaret plant the tulips", known={"Margaret wants to plant tulips in spring."})
    assert not any("tulips" in o for o in q.out), "memory already in the prompt was re-sent"
    for chit_chat in ("turn on the radio please", "thank you Ember", "what time is it", "good night"):
        q, _ = await recall(chit_chat)
        assert q.out == [], f"{chit_chat!r} recalled: {q.out}"
    print("9 recall timing ok")

    # 10. Someone new: asked who they are (only once Ember is quiet), remembered
    #     through Gemini's tool call, then named.
    R._face_id._last_intro = -1e9
    with R._playback_lock: R._playback_buffer = b"\x00" * 10          # Ember still talking
    scene["photos"] = [stranger[5:]]
    await asyncio.sleep(6)
    assert not of("[NEW PERSON]"), "asked over Ember's own speech"
    with R._playback_lock: R._playback_buffer = b""
    try:
        await wait_for(lambda: of("[NEW PERSON]"), what="stranger asked")
    except AssertionError:
        now = time.time()
        for t in R._face_id._tracks:
            print("DBG track", t.id, "age", round(now - t.first_seen, 1), "seen", round(now - t.last_seen, 1),
                  "person", t.person_id, "maybe", t.maybe_id, "asked", t.asked, "declined", t.declined,
                  "samples", len(t.samples))
        print("DBG last_intro", round(now - R._face_id._last_intro, 1), "visitors", len(R._face_id._visitors))
        raise
    intro = [(m, c) for m, c in sent if m.startswith("[NEW PERSON]")][-1]
    assert intro[1] is True and "ask if it's okay to remember their face" in intro[0]
    await s.q.put(types.LiveServerMessage(tool_call=types.LiveServerToolCall(function_calls=[
        types.FunctionCall(id="r1", name="remember_person", args={"name": "Ruth", "relationship": "neighbor"})])))
    await wait_for(lambda: "Ruth (Margaret's neighbor) is in front of you." in
                   ([m for m, _ in sent if m.startswith("[SELF]")] or [""])[-1], what="Ruth named")
    print("10 stranger asked, remembered, named ok")
    for t in tasks: t.cancel()
    print("PASS")

asyncio.run(main())
