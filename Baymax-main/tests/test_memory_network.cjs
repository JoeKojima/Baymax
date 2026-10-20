const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const html=fs.readFileSync(path.join(__dirname,'../static/memory-palace.html'),'utf8');
const script=html.match(/<script>([\s\S]*?)<\/script>/)[1];
new vm.Script(script);
const modelCode=script.slice(script.indexOf('function shortDate('),script.indexOf('function renderNetwork('));
const model=vm.runInNewContext(modelCode+'; networkModel');
const data={people:[{name:'Alex',id:'a'},{name:'Alex',id:null},{name:'Sam',id:'s'}],memories:[
{id:'1',people:['Alex'],text:'Piano',kind:'fact',time:1},
{id:'2',people:['Alex','Sam'],text:'Concert',kind:'conversation',time:2,recalled:true},
{id:'3',people:[],text:'Unscoped',kind:'fact',time:3}],graph:{claims:[
{id:'c1',owner_id:'a',kind:'interest',value:'Music',evidence:[{quote:'I like music'}]},
{id:'c2',owner_id:'s',kind:'interest',value:'Surfing',evidence:[]},
{id:'c3',owner_id:null,kind:'interest',value:'Unscoped idea',evidence:[]}]}};
assert.equal(model(data,'Alex').total,3);
assert.equal(model(data,'Nobody').total,0); // A name without an ID never acquires unowned claims.
assert.equal(model(data,'?').total,2);
assert.equal(model(data,'Alex','music').rows[0].id,'claim:c1');
assert.equal(model(data,'Alex','','shared').rows[0].id,'saved:2');
assert.equal(model(data,'Alex','','context').total,1);
assert.equal(model(data,'Sam').total,2); // Shared notes retain both links.
const many={...data,memories:Array.from({length:100},(_,i)=>({...data.memories[0],id:String(i),time:i}))};
assert.equal(model(many,'Alex').rows.length,40);
assert.equal(model(many,'Alex').total,101);
assert.equal(model(many,'Alex').rows[0].original,'99');
console.log('PASS: network person isolation, shared links, evidence search, filters, bounds and JS syntax.');

const bubbleCode=script.slice(script.indexOf('const THEMES='),script.indexOf('function renderNetwork('));
const bubble=vm.runInNewContext(bubbleCode+'; ({bubbleModel,bubbleLayout,themesFor,filterEncoding,lensGroups,emotionBucket})');
const scoped=model(data,'Alex');
const shallow=bubble.bubbleModel(scoped,1);
assert(!shallow.nodes.some(n=>n.type==='memory'));
assert(shallow.nodes.some(n=>n.type==='theme'&&n.label==='Music'));
const deep=bubble.bubbleModel(scoped,3);
assert(deep.nodes.some(n=>n.type==='evidence'));
assert(deep.nodes.filter(n=>n.type==='memory').every(n=>scoped.rows.some(r=>r.id===n.id)));
const points=bubble.bubbleLayout(deep);
assert(points.every(n=>Number.isFinite(n.x)&&Number.isFinite(n.y)));
assert.deepEqual(Array.from(points,n=>[n.x,n.y]),Array.from(bubble.bubbleLayout(deep),n=>[n.x,n.y]));
const focused=bubble.bubbleModel(scoped,2,'Music');
assert(focused.rows.every(r=>bubble.themesFor(r).some(t=>t.name==='Music')));
console.log('PASS: bubble depths, theme grouping, source nodes, stable bounded layout and scoped focus.');

const labels=vm.runInNewContext(script.slice(script.indexOf('function shortDate('),script.indexOf('function networkModel('))+'; ({shortDate,displayNote,shortNodeLabel})');
assert.equal(labels.displayNote('Sunday, October 18, 2026'), '10/18/26');
assert.equal(labels.displayNote('Oct 18'), '10/18');
assert.equal(labels.displayNote('2026-10-18'), '10/18/26');
assert.equal(labels.shortDate(0),'');
assert.equal(labels.shortDate(new Date(2026,9,18).getTime()/1000),'10/18/26');
assert.equal(labels.shortNodeLabel({type:'memory',label:'plays the piano and likes to create piano covers'}),'piano');
assert.equal(labels.shortNodeLabel({type:'evidence',label:'Saved source: a very long source quote',row:{time:0}}),'Source');
assert(!labels.shortNodeLabel({type:'evidence',label:'Sunday, October 18, 2026',row:{time:1}}).includes('Sunday'));
console.log('PASS: concise node labels and numeric dates; original source text stays outside bubble labels.');

const hints={links:[{source:'saved:1',target:'claim:c1',relation:'similarity_hint'},{source:'saved:1',target:'claim:c2',relation:'similarity_hint'}]};
assert.equal(bubble.bubbleModel(scoped,2,null,hints).links.filter(l=>l.suggested).length,1);
assert.equal(bubble.bubbleModel(scoped,1,null,hints).links.filter(l=>l.suggested).length,0);
console.log('PASS: sweeper links are limited to visible, person-scoped memory nodes.');

const duplicates={...data,memories:[...data.memories,{...data.memories[0],id:'copy',time:4}],graph:{...data.graph,sweep:{groups:[{type:'exact_duplicate',members:['saved:1','saved:copy']}]}}};
assert.equal(model(duplicates,'Alex').total,3);
assert.equal(model(duplicates,'Alex').sourceTotal,4);
assert.equal(model(duplicates,'Alex').rows.find(r=>r.id==='saved:copy').support_ids.length,2);
console.log('PASS: exact copies collapse to one network node with both source IDs retained.');

const encodings={people:[{id:'a',name:'Alex'},{id:'b',name:'Sam'}],memories:[{id:'old',text:'Likes piano.',people:['Alex'],time:1}],graph:{claims:[],interaction_events:[
{id:'happy',owner_id:'a',t:10,text:'I feel happy about piano.',interaction:{emotions:[{label:'happy',valence:1,target:'piano'}],feedback:[],metrics:{word_count:6,response_gap_proxy_ms:1200}}},
{id:'sad',owner_id:'a',t:11,text:'I feel sad about work.',interaction:{emotions:[{label:'sad',valence:-1,target:'work'}],feedback:[{kind:'timing_boundary'}],metrics:{word_count:6,response_gap_proxy_ms:12000}}},
{id:'other',owner_id:'b',t:12,text:'Happy.',interaction:{emotions:[{label:'happy',valence:1}],feedback:[],metrics:{word_count:1}}}]}};
assert.equal(model(encodings,'Alex').total,1); // Topic mode stays about stored notes/claims.
const emotionRows=model(encodings,'Alex','','all','emotion');
assert.equal(emotionRows.total,3);
assert(!emotionRows.rows.some(r=>r.id==='event:other'));
assert.equal(bubble.filterEncoding(emotionRows,'emotion',1,false).rows[0].id,'event:happy');
assert.equal(bubble.filterEncoding(emotionRows,'emotion',0,true).rows.length,3);
assert.equal(bubble.filterEncoding(emotionRows,'gap',2,false).rows[0].id,'event:happy');
assert.equal(bubble.filterEncoding(emotionRows,'length',10,false).rows.length,0);
assert.equal(bubble.filterEncoding(emotionRows,'feedback',2,false).rows[0].id,'event:sad');
const emotionGraph=bubble.bubbleModel(emotionRows,2,null,{},'emotion');
assert(emotionGraph.themes.some(t=>t.name==='Emotion unmeasured'));
assert(emotionGraph.themes.some(t=>t.name==='happy'));
assert.equal(bubble.emotionBucket({interaction:{emotions:[{valence:1},{valence:-1}]}}),'mixed');
console.log('PASS: emotion/timing/length/feedback lenses, unknown handling and episode owner isolation.');
