// Execute the shipped UI against a DOM/API double; no document upload or AI request.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const profiles = JSON.parse(process.argv[2]);
const elements = new Map(), created = [], requests = [];
class Element {
  constructor(tag) { this.tagName=tag;this.children=[];this.value="";this.checked=false;this.disabled=false;this.hidden=false;this.dataset={};this.listeners={};this.textContent="";this.classList={toggle(){},remove(){},add(){}}; }
  append(...children) { for(const child of children){this.children.push(child);child.parent=this;} }
  replaceChildren(...children) { this.children=[];this.append(...children); }
  setAttribute(name,value) { this[name]=value; }
  addEventListener(name,handler) { this.listeners[name]=handler; }
  closest(selector) { for(let item=this;item;item=item.parent){if(selector?.startsWith(".")?(item.className||"").split(" ").includes(selector.slice(1)):item.tagName===selector)return item;}return get("label-for-"+this.id); }
  focus() { this.focused=true; }
  before() {}
}
function get(id) { const dynamic=created.findLast(el=>el.id===id);if(dynamic)return dynamic;if(!elements.has(id)){const el=new Element("div");el.id=id;elements.set(id,el);}return elements.get(id); }
function descendants(el) { return el.children.flatMap(child=>[child,...descendants(child)]); }
function text(el) { return [el.textContent,...el.children.map(text)].join(" "); }
function button(el,label) { return descendants(el).find(child=>child.tagName==="button"&&child.textContent===label); }
get("confirmation-form").append(get("contract-fields"),get("manual-fields"),get("material-review"));
const field=(field_id,raw_value,suggested_value,validation_status,reasons=[],refs=[])=>({field_id,raw_value,suggested_value,normalized_value:validation_status==="valid"?suggested_value:null,validation_status,validation_reasons:reasons,unit:"minute",issues:[],evidence_refs:refs});
const mismatch=field("break_minutes","1 시간","60","needs_review",["SOURCE_QUOTE_MISMATCH"],[]);
mismatch.source_quote="실제 OCR에 없는 문장 <script>alert(1)</script>";mismatch.source_page=99;
const extraction={verification_method:"ocr_unverified",fields:[field("contract_rate","3,000,000","3000000","valid",[],["valid-evidence"]),mismatch,field("contract_end",null,null,"missing")],evidence_catalog:[{evidence_id:"valid-evidence",source_id:"main-pdf",page:2,quote:"월급 3,000,000",verification_method:"ocr_unverified"}],issues:[]};
let savedCase={id:"review-case",document_id:"main-pdf",document_type:"contract",jobs:[],dirty:true,confirmed_revision:null,analysis:null,ocr:{pages:["원문 첫 페이지","월급 3,000,000"]},extraction,draft:{values:{contract_rate:"3000000",break_minutes:null},manual:{},contract_unit:"KRW/month",slip_unit:"",period_start:"",period_end:"",time_kind:"",material_review:{}}};
const responses={"/api/extraction-profiles":profiles,"/api/health":{model:"test"},"/api/documents":{documents:[]},"/api/comparison-sources":{sources:[]}};
const context=vm.createContext({
  document:{body:{dataset:{}},getElementById:get,createElement:tag=>{const el=new Element(tag);created.push(el);return el;},querySelectorAll:selector=>selector.startsWith("#confirmation-form")?descendants(get("confirmation-form")).filter(el=>["input","select"].includes(el.tagName)):[]},
  fetch:async(url,options)=>{requests.push({url,method:options.method,body:options.body?JSON.parse(options.body):null});let result;if(options.method==="PUT"){assert.equal(url,"/api/cases/review-case/draft");savedCase={...savedCase,draft:JSON.parse(options.body)};result=savedCase;}else{assert.equal(options.method,"GET");assert.ok(url in responses,url);result=responses[url];}return {ok:true,json:async()=>structuredClone(result)};},
  localStorage:{getItem:()=>null,setItem(){},removeItem(){}},location:{search:"",pathname:"/ai"},history:{replaceState(){},pushState(){}},window:{addEventListener(){}},URLSearchParams,setTimeout(){},clearTimeout(){},
});
let source=fs.readFileSync(path.join(__dirname,"../../dev/web/app.js"),"utf8");
source=source.replace(/initialize\(\)\.catch\(error=>\{[^\n]+\}\);\s*$/, "globalThis.uiInitialized=initialize();");
vm.runInContext(fs.readFileSync(path.join(__dirname,"../../dev/web/results.js"),"utf8"),context);
vm.runInContext(fs.readFileSync(path.join(__dirname,"../../dev/web/compare.js"),"utf8"),context);
vm.runInContext(fs.readFileSync(path.join(__dirname,"../../dev/web/pages.js"),"utf8"),context);
vm.runInContext(source,context);
const run=code=>vm.runInContext(code,context);
(async()=>{
  await context.uiInitialized;
  context.fixture=structuredClone(savedCase);run("current=fixture;renderCase()");
  assert.equal(get("ai-state").textContent,"항목 정리 완료 · 1개 확인 필요");
  const candidates=text(get("document-extraction"));
  for(const label of ["원문 표기 후보","AI 제안","검사 통과 · 사용자 확인 전","확인 필요","미기입","1 시간","60","AI가 인용한 문장 · OCR 원문 일치 미확인"])assert.ok(candidates.includes(label),label);
  assert.ok(candidates.includes("모든 항목은 사용자 확인 전 후보"));
  assert.equal(descendants(get("document-extraction")).some(el=>el.tagName==="script"),false,"Untrusted source values are rendered as text.");
  assert.equal(get("value-break_minutes").value,"","A rejected suggestion never populates the comparison input.");
  assert.equal(get("value-contract_rate").value,"3000000","Usable candidates still follow the existing confirmation flow.");
  assert.ok(text(get("meta-break_minutes")).includes("인용문을 저장된 원문에서 확인하지 못했습니다."));
  assert.equal(descendants(get("meta-break_minutes")).some(el=>el.tagName==="button"&&/99쪽/.test(el.textContent)),false,"An unverified AI page is never an evidence link.");
  button(get("meta-break_minutes"),"원본 PDF 보기 ↗").listeners.click();
  assert.equal(get("pdf").src,"/preview.html?document=main-pdf#page=1");
  descendants(get("meta-contract_rate")).find(el=>el.tagName==="button"&&el.textContent.includes("2쪽")).listeners.click();
  assert.equal(get("pdf").src,"/preview.html?document=main-pdf#page=2");
  button(get("meta-break_minutes"),"직접 입력").listeners.click();assert.equal(get("value-break_minutes").focused,true);
  get("value-break_minutes").value="30";run("changed()");await run("flushDraft()");run("renderCase()");
  assert.equal(get("value-break_minutes").value,"30","A user's correction survives rendering while the original warning remains visible.");
  button(get("meta-break_minutes"),"보류 · 빈값으로 두기").listeners.click();await run("flushDraft()");
  assert.equal(requests.at(-1).body.values.break_minutes,null,"Deferring a field persists null, not the AI suggestion or zero.");
  assert.equal(get("confirm").disabled,false,"An uncertain field does not fail or block the entire usable document.");
  run("currentJob={stage:'extract',status:'completed',usage:{total_tokens:100}};renderJob()");
  assert.ok(get("extraction-job-status").textContent.includes("항목 정리 완료 · 1개 확인 필요"));
  run("current.extraction_changes=[{group:'values',field_id:'break_minutes',preserved:'30',candidate:null}];renderExtractionChanges()");
  assert.equal(button(get("extraction-changes"),"새 추출값 사용"),undefined,"Re-extraction cannot offer a rejected candidate as an input shortcut.");
  const linkedField={...mismatch,field_id:"gross",source_document_id:"slip-pdf",source_title:"명세서"};
  context.linkedField=linkedField;
  run("currentJob=null;current.extraction_changes=[];current.materials={selection:{payslip_case_id:'slip-case',deposit_case_ids:['bank-case']},sources:[{case_id:'slip-case',document_id:'slip-pdf'},{case_id:'bank-case',document_id:'bank-pdf'}],field_validation:{gross:linkedField},field_evidence:{},evidence_catalog:[],earnings:[{item_id:'row-1',source_case_id:'slip-case',label:'기본급',amount:null,evidence_refs:[],validation_warnings:[{...linkedField,field_id:'earning_1_amount',label:'금액'}]}],deductions:[],transactions:[{transaction_id:'tx-1',source_case_id:'bank-case',date:null,amount:null,transaction_amount:null,evidence_refs:[],validation_warnings:[{...linkedField,field_id:'transaction_1_deposit_amount',source_document_id:'bank-pdf',label:'입금액'}]}]};renderCase()");
  assert.ok(text(get("manual-meta-gross")).includes("AI 제안"));assert.equal(get("manual-gross").value,"");
  button(get("manual-meta-gross"),"원본 PDF 보기 ↗").listeners.click();assert.equal(get("pdf").src,"/preview.html?document=slip-pdf#page=1","Linked invalid evidence opens its own original, never the anchor document.");
  assert.equal(run("materialInputs.earnings.get('row-1').value"),"");assert.equal(run("materialInputs.transactions.get('tx-1').amount.value"),"");
  const warnings=descendants(get("material-review")).filter(el=>el.className==="material-validation-warnings");assert.equal(warnings.length,2);
  run("materialInputs.transactions.get('tx-1').selected.checked=true;materialInputs.transactions.get('tx-1').transaction_amount.value='100';materialInputs.transactions.get('tx-1').amount.value='50'");
  button(warnings[1],"보류 · 빈값으로 두기").listeners.click();
  const review=run("readMaterialReview()");assert.equal(review.transactions["tx-1"].selected,false);assert.equal(review.transactions["tx-1"].transaction_amount,null);assert.equal(review.transactions["tx-1"].amount,null);
  run("current.materials.earnings[0].validation_warnings[0].field_id='earning_1_label';current.materials.transactions[0].validation_warnings[0].field_id='transaction_1_date';current.draft.material_review.transactions={'tx-1':{selected:true,amount:'50',transaction_amount:'100'}};renderMaterialReview();updateControls()");
  assert.equal(run("materialInputs.row_roles.get('row-1').value"),"detail","A problematic statement row is not silently excluded from completeness checks.");
  button(get("material-review"),"이 행 제외").listeners.click();assert.equal(run("materialInputs.row_roles.get('row-1').value"),"excluded","Exclusion requires an explicit user action.");
  assert.equal(run("materialInputs.transactions.get('tx-1').selected.checked"),false);assert.equal(run("materialInputs.transactions.get('tx-1').selected.disabled"),true,"Invalid transaction identity cannot be selected, including after updateControls.");
  assert.equal(descendants(get("material-review")).filter(el=>el.tagName==="button"&&el.textContent==="직접 입력").length,0,"A non-editable date or label cannot misleadingly focus an amount input.");
  run("current.materials.transactions[0].validation_warnings[0].validation_status='missing';current.materials.transactions[0].validation_warnings[0].validation_reasons=['REQUIRED_VALUE_MISSING'];renderMaterialReview();updateControls()");
  assert.equal(run("materialInputs.transactions.get('tx-1').selected.disabled"),true,"A missing required date is also withheld from selection.");
  assert.ok(text(get("material-review")).includes("대조에 필요한 값이 없습니다."));
  assert.ok(requests.every(request=>request.method!=="POST"),"Review and deferral never initiate AI or analysis.");
  console.log("Field candidates, warnings, safe source navigation, manual corrections and deferral verified.");
})().catch(error=>{console.error(error);process.exitCode=1;});
