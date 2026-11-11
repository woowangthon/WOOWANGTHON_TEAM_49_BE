// Execute the actual app with an in-memory DOM/API. No browser, files uploaded, or model calls.
const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");
const elements = new Map(), created = [], requests = [], timers = new Map();
let timerId = 0;
class Element {
  constructor(tag) { this.tagName=tag;this.children=[];this.value="";this.checked=false;this.disabled=false;this.hidden=false;this.dataset={};this.listeners={};this.textContent="";this.classList={toggle(){},remove(){},add(){}}; }
  append(...children) { for(const child of children){this.children.push(child);child.parent=this;} }
  replaceChildren(...children) { this.children.forEach(child=>child.parent=null);this.children=[];this.append(...children); }
  setAttribute(name,value) { this[name]=value; }
  addEventListener(name,handler) { this.listeners[name]=handler; }
  closest(selector) { for(let current=this;current;current=current.parent){if(selector?.startsWith(".")?(current.className||"").split(" ").includes(selector.slice(1)):current.tagName===selector)return current;}return this.parent||get(this.id==="upload"?"upload-label":"label-for-"+this.id); }
  before() {}
}
function get(id) {
  const dynamic=created.findLast(el=>el.id===id);if(dynamic)return dynamic;
  if(!elements.has(id)){const el=new Element(id.includes("unit")||id==="document-type"?"select":"div");el.id=id;elements.set(id,el);}
  return elements.get(id);
}
function descendants(el) { return el.children.flatMap(child=>[child,...descendants(child)]); }
function text(el) { return [el.textContent,...el.children.map(text)].join(" "); }
get("filter").value="all";
get("confirmation-form").append(get("material-review"),get("manual-fields"),get("contract-fields"));
const profiles=Object.fromEntries(["contract","attendance","payslip","deposit","payslip_ocr"].map(key=>[key,{label:key,fields:{},rows:{}}]));
const sources=[{case_id:"contract-1",document_id:"contract-pdf",title:"계약서 A",document_type:"contract",has_extraction:true},{case_id:"work-1",document_id:"work-pdf",title:"근무기록 A",document_type:"attendance",has_extraction:true},{case_id:"slip-ocr-1",document_id:"slip-pdf",title:"명세서 OCR",document_type:"payslip_ocr",has_extraction:true},{case_id:"bank-1",document_id:"bank-pdf",title:"은행 거래",document_type:"deposit",has_extraction:true},{case_id:"bank-empty",document_id:"empty-pdf",title:"추출 전 입금자료",document_type:"deposit",has_extraction:false}];
const selected={contract_case_id:"contract-1",attendance_case_ids:["work-1"],payslip_case_id:"slip-ocr-1",deposit_case_ids:["bank-1"]};
const materials={selection:selected,sources:sources.filter(s=>s.has_extraction).map(s=>({...s,ref:{source_id:"source-"+s.case_id}})),earnings:[{item_id:"earning-1",label:"기본급",amount:null,evidence_refs:["slip-ev"]}],deductions:[{item_id:"deduction-1",label:"공제",amount:"200000",evidence_refs:["slip-ev"]}],transactions:[{transaction_id:"bank-tx",date:"2026-10-31",description:"급여",amount:"2800000",transaction_amount:"2800000",evidence_refs:["bank-ev"]}],evidence_catalog:[{evidence_id:"slip-ev",source_id:"source-slip-ocr-1",page:1,quote:"기본급 3,000,000",verification_method:"ocr_unverified"},{evidence_id:"bank-ev",source_id:"source-bank-1",page:2,quote:"급여 2,800,000",verification_method:"text_match"}],warnings:["원문 확인 필요"]};
const draft={values:{contract_rate:"3000000"},manual:{},contract_unit:"KRW/month",slip_unit:"KRW/month",period_start:"2026-10-01",period_end:"2026-10-31",time_kind:"",earnings_complete:false,deductions_complete:false,payment_scope_confirmed:false,synthetic_auxiliary:false,material_review:{}};
let savedCase={id:"main",document_id:"work-pdf",document_type:"attendance",draft,materials,jobs:[],dirty:true,confirmed_revision:null,analysis:null};
const responses={"/api/extraction-profiles":profiles,"/api/health":{model:"test-model"},"/api/documents":{documents:[]},"/api/comparison-sources":{sources}};
const context=vm.createContext({
  document:{body:{dataset:{}},getElementById:get,createElement:tag=>{const el=new Element(tag);created.push(el);return el;},querySelectorAll:selector=>selector.startsWith("#confirmation-form")?descendants(get("confirmation-form")).filter(el=>["input","select"].includes(el.tagName)):[]},
  fetch:async(url,options)=>{requests.push({url,method:options.method,body:options.body?JSON.parse(options.body):null});let result;
    if(url==="/api/cases/main/materials"&&options.method==="PUT"){const selection=JSON.parse(options.body),hasSelection=Object.values(selection).some(value=>Array.isArray(value)?value.length:Boolean(value));savedCase={...savedCase,materials:hasSelection?{...materials,selection}:null,materials_invalid:false};result=savedCase;}
    else if(url==="/api/cases/main/draft"&&options.method==="PUT"){savedCase={...savedCase,draft:JSON.parse(options.body)};result=savedCase;}
    else{assert.equal(options.method,"GET");assert.ok(url in responses,"Unexpected API: "+url);result=responses[url];}
    return {ok:true,json:async()=>structuredClone(result)};},
  localStorage:{getItem:()=>null,setItem(){},removeItem(){}},location:{search:"",pathname:"/ocr"},history:{replaceState(){},pushState(){}},window:{addEventListener(){}},URLSearchParams,
  setTimeout:fn=>{timers.set(++timerId,fn);return timerId;},clearTimeout:id=>timers.delete(id),
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
  context.fixture=structuredClone(savedCase);run("current=fixture;renderCase()");await run("loadComparisonSources()");
  context.contractAnchorFixture=structuredClone(savedCase);
  context.contractAnchorFixture.document_id="contract-pdf-A";
  context.contractAnchorFixture.document_type="contract";
  context.contractAnchorFixture.extraction={fields:[{field_id:"contract_rate",raw_value:"1110000",normalized_value:"1110000",unit:"KRW/hour",evidence_refs:["contract-a-ev"]}],evidence_catalog:[{evidence_id:"contract-a-ev",source_id:"source-anchor-a",page:1,quote:"A 계약서 시급 1,110,000원",verification_method:"text_match"}]};
  context.contractAnchorFixture.materials.sources.find(item=>item.case_id==="contract-1").document_id="contract-pdf-B";
  context.contractAnchorFixture.materials.field_evidence={contract_rate:["contract-b-ev"]};
  context.contractAnchorFixture.materials.evidence_catalog.push({evidence_id:"contract-b-ev",source_id:"source-contract-1",page:3,quote:"B 계약서 월급 3,000,000원",verification_method:"text_match"});
  run("current=contractAnchorFixture;renderCase()");
  assert.equal(get("value-contract_rate").value,"3000000");assert.ok(text(get("meta-contract_rate")).includes("KRW/month"));
  descendants(get("meta-contract_rate")).find(el=>el.tagName==="button").listeners.click();
  assert.equal(get("pdf").src,"/preview.html?document=contract-pdf-B#page=3","Selected contract B supplies the evidence even when the main document is contract A.");
  assert.equal(get("evidence-quote").textContent,"B 계약서 월급 3,000,000원");
  run("current.materials.field_evidence={};renderCase()");
  assert.equal(descendants(get("meta-contract_rate")).filter(el=>el.tagName==="button").length,0,"Missing linked evidence must not fall back to the unrelated main contract A.");
  run("current.materials.field_evidence={contract_rate:['contract-b-ev']}");
  run("current.document_type='payslip';current.extraction={fields:[],evidence_catalog:[]};renderCase()");
  descendants(get("meta-contract_rate")).find(el=>el.tagName==="button").listeners.click();
  assert.equal(get("pdf").src,"/preview.html?document=contract-pdf-B#page=3","Linked contract evidence also works when the main document is a payslip.");
  run("current=fixture;renderCase()");
  assert.equal(get("contract-inputs").hidden,false,"Linked contract inputs are visible when the main file is attendance.");
  const selectors=run("materialSelectors");
  assert.equal(selectors.payslip_case_id.value,"slip-ocr-1");
  assert.ok(selectors.payslip_case_id.children.some(option=>option.value==="slip-ocr-1"));
  assert.equal(selectors.deposit_case_ids.find(input=>input.value==="bank-empty").disabled,true);
  assert.equal(run("materialInputs.transactions.get('bank-tx').selected.checked"),false,"Bank transactions are never selected automatically.");
  assert.equal(run("materialInputs.earnings.get('earning-1').value"),"","Missing amounts stay blank.");
  run("materialInputs.earnings.get('earning-1').value='3000000';materialInputs.transactions.get('bank-tx').selected.checked=true;materialInputs.transactions.get('bank-tx').amount.value='1400000'");
  const form=run("readForm()");
  assert.equal(form.material_review.earnings["earning-1"],"3000000");
  assert.equal(form.material_review.transactions["bank-tx"].selected,true);
  assert.equal(form.material_review.transactions["bank-tx"].amount,"1400000");
  assert.equal(form.material_review.transactions["bank-tx"].transaction_amount,"2800000");
  assert.equal(form.material_review.distinct_transactions_confirmed,false);
  assert.equal(form.earnings_complete,false);assert.equal(form.payment_scope_confirmed,false);
  run("changed()");await run("flushDraft()");
  assert.equal(requests.find(r=>r.url.endsWith("/draft")).body.material_review.transactions["bank-tx"].amount,"1400000");
  run("renderCase()");
  assert.equal(run("materialInputs.transactions.get('bank-tx').selected.checked"),true,"Saved transaction selection survives rendering.");
  assert.equal(run("materialInputs.transactions.get('bank-tx').amount.value"),"1400000");
  run("current.materials.transactions[0].duplicate_candidate_group='duplicate-group';current.materials.transactions.push({...current.materials.transactions[0],transaction_id:'bank-tx-two',source_case_id:'another-bank-file'},{transaction_id:'bank-unclear',date:'2026-10-31',amount:null,transaction_amount:null,evidence_refs:[]});renderMaterialReview();updateControls()");
  assert.equal(run("materialInputs.transactions.get('bank-unclear').transaction_amount.value"),"","Unknown original deposits stay blank and can be corrected from the source.");
  run("materialInputs.transactions.get('bank-unclear').transaction_amount.value='90';materialInputs.transactions.get('bank-unclear').amount.value='45';materialInputs.transactions.get('bank-unclear').selected.checked=true");
  assert.ok(text(get("material-review")).includes("중복 후보를 각각 별개의 거래로 확인"));
  assert.equal(run("duplicateTransactionReviewNeeded()"),false,"Selecting only one duplicate candidate needs no extra acknowledgment.");
  run("materialInputs.transactions.get('bank-tx-two').selected.checked=true;changed()");
  assert.equal(get("confirm").disabled,true,"Two selected duplicate candidates require explicit distinct-transaction review.");
  run("distinctTransactionsInput.checked=true;changed()");
  assert.equal(get("confirm").disabled,false);assert.equal(run("readMaterialReview().distinct_transactions_confirmed"),true);
  savedCase.materials=structuredClone(run("current.materials"));
  await run("flushDraft()");run("renderCase()");
  assert.equal(run("distinctTransactionsInput.checked"),true,"Distinct-transaction review survives saving and rendering.");
  assert.equal(run("materialInputs.transactions.get('bank-tx-two').selected.checked"),true);
  assert.equal(run("materialInputs.transactions.get('bank-unclear').transaction_amount.value"),"90");assert.equal(run("materialInputs.transactions.get('bank-unclear').amount.value"),"45");
  const corrected=requests.filter(r=>r.url.endsWith("/draft")).at(-1).body.material_review.transactions["bank-unclear"];
  assert.deepEqual(corrected,{selected:true,amount:"45",transaction_amount:"90"});
  context.dynamicFixture=structuredClone(run("current"));
  context.dynamicFixture.draft.material_review={};
  context.dynamicFixture.materials.transactions=[{transaction_id:"correct-a",date:"2026-10-31",amount:"90",transaction_amount:"90",duplicate_candidate_group:null,evidence_refs:[]},{transaction_id:"correct-b",date:"2026-10-31",amount:"9",transaction_amount:"9",duplicate_candidate_group:null,evidence_refs:[]}];
  run("globalThis.previousComparisonCase=current;current=dynamicFixture;renderMaterialReview();materialInputs.transactions.get('correct-a').selected.checked=true;materialInputs.transactions.get('correct-b').selected.checked=true;updateControls()");
  assert.ok(run("distinctTransactionsInput"),"Two transactions expose distinct-transaction review even without saved duplicate hints.");
  assert.equal(get("confirm").disabled,false,"Different effective original amounts do not require duplicate review.");
  run("materialInputs.transactions.get('correct-b').transaction_amount.value='90';changed()");
  assert.equal(get("confirm").disabled,true,"Correcting 9 to 90 dynamically creates a duplicate candidate with the existing 90 deposit.");
  run("distinctTransactionsInput.checked=true;changed()");assert.equal(get("confirm").disabled,false);
  run("distinctTransactionsInput.checked=false;materialInputs.transactions.get('correct-b').transaction_amount.value='9';updateControls()");
  assert.equal(get("confirm").disabled,false,"Correcting values to distinct amounts resolves duplicate review without a stale fixed-group block.");
  run("materialInputs.transactions.get('correct-b').transaction_amount.value='90';current.materials.transactions[0].account_number='account-a';current.materials.transactions[1].account_number='account-b';updateControls()");
  assert.equal(get("confirm").disabled,false,"Different known accounts distinguish otherwise matching deposits.");
  run("current.materials.transactions[1].account_number='account-a';current.materials.transactions[0].reference='ref-a';current.materials.transactions[1].reference='ref-b';updateControls()");
  assert.equal(get("confirm").disabled,false,"Different explicit references in the same known account distinguish deposits.");
  run("current=previousComparisonCase;pendingDraft=false;clearTimeout(draftTimer);renderMaterialReview()");
  run("showEvidence(current.materials.evidence_catalog.find(e=>e.evidence_id==='bank-ev'))");
  assert.equal(get("pdf").src,"/preview.html?document=bank-pdf#page=2");
  run("current.analysis={comparisons:[{rule_id:'R-06',status:'difference',lhs:'2800000',rhs:'1400000',delta:'1400000',unit:'KRW',evidence_refs:['bank-ev']}],evidence_catalog:current.materials.evidence_catalog,questions:[]};current.analysis_period={start:'2026-10-01',end:'2026-10-31'};current.draft.period_start='2026-11-01';renderResults()");
  const results=text(get("comparison-results"))+text(get("result-overview"));assert.ok(results.includes("선택·배분한 입금액"));assert.ok(results.includes("2026-10-01"));assert.ok(!results.includes("2026-11-01"));
  run("current.analysis.comparisons=[{rule_id:'R-01',status:'difference',lhs:'14000',rhs:'13000',delta:'1000',unit:'KRW/hour',evidence_refs:[]}];renderResults()");
  assert.ok(text(get("comparison-results")).includes("명세서 단가: 14,000"));assert.ok(text(get("comparison-results")).includes("계약서 단가: 13,000"));
  run("currentJob={id:'old-extraction',stage:'extract',status:'failed',error:'AI가 추출한 값의 표기를 OCR 원문에서 확인하지 못했습니다. (사유: SOURCE_VALUE_MISMATCH · 항목: 휴게 시간 (분) [break_minutes])',error_code:'MODEL_OUTPUT_INVALID',usage:{total_tokens:11650}};renderJob();switchSection('results')");
  assert.ok(!get("job-status").textContent.includes("MODEL_OUTPUT_INVALID"),"Extraction failures are displayed in the extraction panel, not as comparison failures.");
  assert.ok(get("development-details").textContent.includes("MODEL_OUTPUT_INVALID"));
  assert.ok(get("development-details").textContent.includes("SOURCE_VALUE_MISMATCH"));
  assert.ok(get("development-details").textContent.includes("[break_minutes]"));
  assert.ok(get("development-details").textContent.includes("old-extraction"));
  assert.ok(get("development-details").textContent.includes("11650"));
  assert.equal(get("resume").hidden,true);assert.equal(get("extraction-resume").hidden,false);
  run("current.materials_invalid=true;pendingDraft=true;draftTimer=setTimeout(()=>{},400);updateControls()");
  assert.equal(get("confirm").disabled,true);assert.equal(get("analyze").disabled,true);
  const previousDraftCalls=requests.filter(r=>r.url.endsWith("/draft")).length;
  await run("applyMaterials()");
  assert.equal(get("compare-page").hidden,false,"Loading materials stays on the comparison page with its value confirmation form.");
  assert.equal(get("manual-section").open,true);
  assert.equal(run("currentJob"),null);assert.equal(get("resume").hidden,true);assert.equal(get("extraction-resume").hidden,true);
  assert.equal(get("extraction-job-status").hidden,true);assert.ok(!get("job-status").textContent.includes("MODEL_OUTPUT_INVALID"));
  assert.equal(requests.filter(r=>r.url.endsWith("/draft")).length,previousDraftCalls,"Invalid linked inputs are refreshed without trying to save an obsolete draft.");
  const materialsRequest=requests.find(r=>r.url.endsWith("/materials"));
  assert.deepEqual(materialsRequest.body,selected);
  assert.ok(requests.filter(r=>r.url==="/api/comparison-sources").length>=2,"Refreshing materials reloads available sources.");
  run("materialSelectors.contract_case_id.value='';materialSelectors.payslip_case_id.value='';for(const input of materialSelectors.attendance_case_ids)input.checked=false;for(const input of materialSelectors.deposit_case_ids)input.checked=false");
  await run("applyMaterials()");
  assert.deepEqual(requests.filter(r=>r.url.endsWith("/materials")).at(-1).body,{contract_case_id:null,attendance_case_ids:[],payslip_case_id:null,deposit_case_ids:[]});
  assert.equal(get("material-review").hidden,true,"Clearing all selections removes linked row controls.");
  assert.ok(requests.every(r=>!r.url.includes("/jobs")),"The UI harness never starts extraction or model work.");
  context.resultFixture=structuredClone(context.fixture);
  context.resultFixture.dirty=false;context.resultFixture.confirmed_revision=3;
  context.resultFixture.analysis_materials=structuredClone(materials);
  context.resultFixture.analysis_period={start:"2026-10-01",end:"2026-10-31"};
  context.resultFixture.analysis={result_id:"new-result",input_version:3,created_at:"2026-11-04T01:00:00Z",questions:[],evidence_catalog:materials.evidence_catalog,comparisons:[
    {rule_id:"R-01",status:"insufficient",lhs:"14000",rhs:null,delta:null,unit:"KRW/hour",reason_codes:["FIELD_UNKNOWN"],evidence_refs:[]},
    {rule_id:"R-02",status:"needs_review",lhs:"9600",rhs:"9600",delta:null,unit:"minute",reason_codes:["PERIOD_MISMATCH"],evidence_refs:[]},
    ...["R-03","R-04","R-05"].map(rule_id=>({rule_id,status:"match",lhs:"2800000",rhs:"2800000",delta:"0",unit:"KRW",reason_codes:[],evidence_refs:["slip-ev"]})),
    {rule_id:"R-06",status:"difference",lhs:"2800000",rhs:"2700000",delta:"100000",unit:"KRW",reason_codes:[],evidence_refs:["slip-ev","bank-ev"]},
    {rule_id:"R-07",status:"needs_review",lhs:null,rhs:null,delta:null,unit:"KRW",reason_codes:[],evidence_refs:[]}
  ]};
  run("current=resultFixture;renderCase();switchSection('results')");
  assert.equal(get("workspace").hidden,true);assert.equal(get("results-view").hidden,false);assert.equal(get("materials-panel").hidden,false);
  assert.equal(get("result-metrics").children.length,4);
  assert.ok(text(get("result-metrics").children[0]).includes("3건"));
  assert.ok(text(get("result-metrics").children[1]).includes("1건"));
  assert.ok(text(get("result-metrics").children[3]).includes("2건"),"R07 now participates in the supported comparison status counts.");
  assert.equal(descendants(get("comparison-results")).filter(el=>el.tagName==="tr"&&el.parent?.tagName==="tbody").length,7);
  assert.ok(text(get("comparison-results")).includes("명세서 OCR"));assert.ok(text(get("comparison-results")).includes("은행 거래"));
  get("result-metrics").children[1].listeners.click();
  assert.equal(descendants(get("comparison-results")).filter(el=>el.tagName==="tr"&&el.parent?.tagName==="tbody").length,1);
  assert.ok(text(get("comparison-results")).includes("입금액이 100,000원 적음"));
  const detailButton=descendants(get("comparison-results")).find(el=>el.tagName==="button");detailButton.listeners.click();
  assert.equal(get("comparison-detail").hidden,false);
  assert.ok(text(get("comparison-detail-body")).includes("선택한 입금의 배분액 합계"));
  const bankEvidence=descendants(get("comparison-detail-body")).find(el=>el.tagName==="button"&&el.textContent.includes("은행 거래"));
  bankEvidence.listeners.click();assert.equal(get("pdf").src,"/preview.html?document=bank-pdf#page=2");assert.equal(get("workspace").hidden,false);
  run("switchSection('results')");get("result-scope").value="terms";get("result-scope").onchange();
  assert.equal(descendants(get("comparison-results")).filter(el=>el.tagName==="tr"&&el.parent?.tagName==="tbody").length,2);assert.ok(text(get("comparison-results")).includes("계약 단가 대조"));
  context.oldResult=structuredClone(context.resultFixture.analysis);context.oldResult.result_id="old-result";context.oldResult.input_version=1;
  context.oldMaterials=structuredClone(materials);context.oldMaterials.sources.find(item=>item.document_type==="deposit").title="이전 은행 원본.pdf";
  run("current.analysis_history=[{analysis:oldResult,period:{start:'2026-09-01',end:'2026-09-30'},materials:oldMaterials}];renderResults()");
  get("result-scope").value="payment";get("result-scope").onchange();get("result-version").value="old-result";get("result-version").onchange();
  assert.ok(text(get("comparison-results")).includes("이전 은행 원본.pdf"));assert.ok(text(get("result-overview")).includes("2026-09-01"));
  run("routeResultId='';resultVersionId='';renderResults();materialSelectors.contract_case_id.value='';materialSelectionChanged()");
  assert.equal(run("materialsSelectionPending()"),true);assert.equal(get("analyze").disabled,true);assert.equal(get("confirm").disabled,true);
  assert.ok(text(get("materials-state")).includes("선택 자료 불러오기"));
  context.partialFixture=structuredClone(context.fixture);
  context.partialFixture.materials.selection={contract_case_id:null,payslip_case_id:null,attendance_case_ids:["work-1"],deposit_case_ids:["bank-1"]};
  context.partialFixture.materials.sources=context.partialFixture.materials.sources.filter(item=>["attendance","deposit"].includes(item.document_type));
  run("current=partialFixture;currentJob=null;renderCase();switchSection('results')");
  assert.ok(text(get("comparison-results")).includes("자료 2건을 불러왔습니다."));
  assert.ok(text(get("comparison-guidance")).includes("직접 확인한 실수령액"));
  assert.ok(text(get("comparison-guidance")).includes("예상 급여를 자동 계산하는 기능은 아직 없습니다"));
  assert.ok(!text(get("comparison-results")).includes("어떤 자료를 비교할까요?"));
  const confirmationButton=descendants(get("comparison-results")).find(el=>el.tagName==="button");
  assert.equal(confirmationButton.disabled,false);confirmationButton.listeners.click();assert.equal(get("compare-page").hidden,false);
  run("current.dirty=false;current.confirmed_revision=20;renderResults()");
  assert.ok(text(get("comparison-results")).includes("대조할 준비가 됐습니다."));assert.equal(get("analyze").disabled,false);
  responses["/api/jobs/old-job"]={id:"old-job",stage:"analyze",status:"failed",document_type:"attendance",revision:19,error_code:"OLD_FAILURE",error:"이전 대조 실패"};
  run("current.jobs=['old-job'];current.materials_revision=20");await run("restoreCurrentJob()");
  assert.equal(run("currentJob"),null,"Restoring a case does not resurrect jobs for materials loaded earlier.");
  assert.equal(get("resume").hidden,true);assert.ok(!get("development-details").textContent.includes("OLD_FAILURE"));
  responses["/api/jobs/old-job"].revision=20;await run("restoreCurrentJob()");
  assert.equal(run("currentJob.id"),"old-job","A failure for the current loaded materials remains visible.");
  assert.ok(get("development-details").textContent.includes("OLD_FAILURE"));assert.equal(get("resume").hidden,false);
  responses["/api/jobs/old-job"].stage="extract";run("delete current.materials_revision");await run("restoreCurrentJob()");
  assert.equal(run("currentJob"),null,"Existing cases without a revision marker do not restore an unrelated terminal extraction after connecting materials.");
  context.rowFixture=structuredClone(context.fixture);
  context.rowFixture.materials.earnings=[{item_id:"detail-pay",label:"기본급",amount:"3000000",row_role:"detail",evidence_refs:["slip-ev"]},{item_id:"subtotal-pay",label:"소계",amount:"3000000",row_role:"subtotal",evidence_refs:["slip-ev"]},{item_id:"total-pay",label:"총지급액",amount:"3000000",row_role:"total",evidence_refs:["slip-ev"]}];
  savedCase=structuredClone(context.rowFixture);
  run("current=rowFixture;currentJob=null;renderCase()");
  assert.equal(run("readMaterialReview().row_roles['subtotal-pay']"),"subtotal");
  assert.ok(text(get("statement-summary-earnings")).includes("3,000,000원"));
  assert.ok(text(get("statement-summary-earnings")).includes("소계·총계 2개 제외"));
  run("materialInputs.row_roles.get('detail-pay').value='excluded';changed()");
  assert.ok(text(get("statement-summary-earnings")).includes("세부항목 없음"));
  run("addManualStatementRow('earnings');globalThis.addedRowId=[...materialInputs.manual_rows.keys()][0];materialInputs.manual_rows.get(addedRowId).label.value='식대';materialInputs.manual_rows.get(addedRowId).amount.value='100000';changed()");
  assert.equal(run("readMaterialReview().manual_rows[0].label"),"식대");
  assert.equal(run("readMaterialReview().manual_rows[0].page"),1);
  assert.ok(text(get("statement-summary-earnings")).includes("100,000원"));
  await run("flushDraft()");run("renderCase()");
  assert.equal(run("materialInputs.manual_rows.get(addedRowId).amount.value"),"100000","User-added rows survive saving and rendering.");
  assert.equal(run("materialInputs.row_roles.get('detail-pay').value"),"excluded");
  run("current.extraction_changes=[{group:'values',field_id:'contract_rate',preserved:'17000',candidate:'3000000'},{group:'',field_id:'contract_unit',preserved:'KRW/hour',candidate:'KRW/month'}];current.draft.values.contract_rate='17000';current.draft.contract_unit='KRW/hour';renderCase()");
  assert.equal(get("extraction-changes").hidden,false);
  const candidateButton=descendants(get("extraction-changes")).find(el=>el.tagName==="button"&&el.textContent==="새 추출값 사용");candidateButton.listeners.click();
  assert.equal(get("value-contract_rate").value,"3000000");assert.equal(get("contract-unit").value,"KRW/month","Choosing a new rate also chooses its matching unit.");
  const keepButton=descendants(get("extraction-changes")).find(el=>el.tagName==="button"&&el.textContent==="유지한 입력 사용");keepButton.listeners.click();
  assert.equal(get("value-contract_rate").value,"17000");assert.equal(get("contract-unit").value,"KRW/hour");
  assert.equal(run("statementAmount(statementCents('9999999999999.99')+statementCents('0.01'))"),"10,000,000,000,000원","Money previews preserve decimal precision.");
  assert.ok(requests.every(r=>!r.url.includes("/jobs")||r.method==="GET"),"The UI regression checks never start model or comparison jobs.");
  console.log("Comparison UI: source roles, linked contract provenance, OCR payslip, corrected deposit principal, partial allocation, distinct-transaction review, persisted review, source PDF mapping, analysis period and invalidation passed.");
})().catch(error=>{console.error(error);process.exitCode=1;});
