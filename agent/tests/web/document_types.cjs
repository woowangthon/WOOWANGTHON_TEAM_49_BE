// Run the actual UI initialization with an in-memory DOM and API, without a browser or uploads.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const vm = require("node:vm");
const path = require("node:path");
const profiles = JSON.parse(process.argv[2]);
const elements = new Map(), created = [];
class Element {
  constructor(tag) { this.tagName=tag; this.children=[]; this.value=""; this.listeners={};this.dataset={};this.classList={remove(){},add(){},toggle(){}}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children=[...children]; }
  setAttribute() {}
  addEventListener(name, handler) { this.listeners[name]=handler; }
  closest() { return get("upload-label"); }
  before() {}
}
function get(id) {
  if(!elements.has(id)) { const el=new Element(id==="document-type"?"select":"div");el.id=id;elements.set(id,el); }
  return elements.get(id);
}
const requests=[];
const responses={"/api/extraction-profiles":profiles,"/api/health":{model:"test-model"},
  "/api/documents":{documents:[]},"/api/comparisons":{comparisons:[]},"/api/comparison-sources":{sources:[]}};
const context=vm.createContext({
  document:{body:{dataset:{}},querySelectorAll:()=>[],getElementById:get,createElement:tag=>{const el=new Element(tag);created.push(el);return el;}},
  fetch:async(url,options)=>{requests.push({url,method:options.method,body:options.body?JSON.parse(options.body):null});if(options.method==="POST"){assert.equal(url,"/api/cases/real-pdf/jobs");return {ok:true,json:async()=>({id:"job-real",stage:JSON.parse(options.body).stage,status:"queued"})};}assert.equal(options.method,"GET");assert.ok(url in responses);return {ok:true,json:async()=>responses[url]};},
  localStorage:{getItem:()=>null,setItem(){},removeItem(){}},location:{search:"",pathname:"/ai"},history:{replaceState(){},pushState(){}},window:{addEventListener(){}},URLSearchParams,
  setTimeout(){},clearTimeout(){},
});
let source=fs.readFileSync(path.join(__dirname,"../../dev/web/app.js"),"utf8");
source=source.replace(/initialize\(\)\.catch\(error=>\{[^\n]+\}\);\s*$/, "globalThis.uiInitialized=initialize();");
assert.ok(source.includes("globalThis.uiInitialized"));
vm.runInContext(fs.readFileSync(path.join(__dirname,"../../dev/web/results.js"),"utf8"),context);
vm.runInContext(fs.readFileSync(path.join(__dirname,"../../dev/web/compare.js"),"utf8"),context);
vm.runInContext(fs.readFileSync(path.join(__dirname,"../../dev/web/pages.js"),"utf8"),context);
vm.runInContext(source,context);
(async()=>{
  await context.uiInitialized;
  const change=get("document-type");
  assert.equal(created.find(el=>el.id==="upload-document-type"),undefined,"Upload does not ask AI type before OCR.");
  const visibleProfiles=["auto",...Object.keys(profiles).filter(kind=>!["payslip_ocr","auto"].includes(kind))];
  for(const select of [change]) {
    assert.deepEqual(select.children.map(option=>option.value),visibleProfiles);
    assert.ok(select.children.some(option=>option.value==="auto"));
    assert.ok(!select.children.some(option=>option.value==="payslip_ocr"));
  }
  // Existing selection survives reloading the same profile list.
  change.value="deposit";
  vm.runInContext('populateDocumentTypes(document.getElementById("document-type"))',context);
  assert.equal(change.value,"deposit");
  assert.equal(change.children.length,visibleProfiles.length);
  // Old OCR-specific records remain usable through the canonical payslip option.
  change.value="payslip_ocr";
  vm.runInContext('populateDocumentTypes(document.getElementById("document-type"))',context);
  assert.equal(change.value,"payslip");
  assert.equal(get("ai-page").hidden,false,"Refreshing /ai retains the AI page.");
  assert.equal(get("ocr-page").hidden,true);assert.equal(get("compare-page").hidden,true);
  assert.equal(get("extract").disabled,true,"AI cannot run before stored OCR exists.");
  assert.equal(requests.some(request=>request.method!=="GET"),false,"Opening any page cannot launch OCR or AI.");
  await vm.runInContext("navigateRoute('compare')",context);assert.equal(get("compare-page").hidden,false);
  await vm.runInContext("navigateRoute('ocr')",context);assert.equal(get("ocr-page").hidden,false);
  assert.equal(requests.some(request=>request.method!=="GET"),false,"Page navigation cannot launch OCR or AI.");
  vm.runInContext("current={id:'real-pdf',document_id:'real-pdf',document_type:'contract',ocr:null,jobs:[],draft:{values:{},manual:{},material_review:{},contract_unit:'KRW/hour',slip_unit:'',period_start:'',period_end:'',time_kind:''}};pollJob=()=>{}",context);
  await assert.rejects(vm.runInContext("start('extract')",context),/원문 읽기/);
  assert.equal(requests.filter(request=>request.method==="POST").length,0);
  await vm.runInContext("start('ocr')",context);
  assert.equal(requests.at(-1).body.stage,"ocr");assert.equal(get("ocr-job-status").hidden,false);
  assert.ok(get("ocr-job-status").textContent.includes("원문 읽기"));
  vm.runInContext("currentJob=null;current.ocr={pages:['real OCR source']}",context);
  await vm.runInContext("navigateRoute('ai')",context);
  assert.equal(requests.filter(request=>request.method==="POST").length,1,"The AI page only navigates after OCR.");
  await vm.runInContext("start('extract')",context);
  assert.equal(requests.at(-1).body.stage,"extract");
  assert.equal(Object.hasOwn(requests.at(-1).body,"use_saved"),false);
  assert.equal(get("extraction-job-status").hidden,false);
  console.log("Three routes separate stored OCR, explicit AI classification and comparison without automatic model calls.");
})().catch(error=>{console.error(error);process.exitCode=1;});
