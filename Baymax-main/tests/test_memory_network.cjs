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
const bubble=vm.runInNewContext(bubbleCode+'; ({bubbleModel,bubbleLayout,themesFor})');
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
