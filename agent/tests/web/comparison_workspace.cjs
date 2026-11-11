// Exercise the shipped independent comparison UI without uploads or model calls.
const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const profiles=JSON.parse(process.argv[2]),elements=new Map(),created=[],requests=[],storage=new Map();
class Element{
  constructor(tag){this.tagName=tag;this.children=[];this.value="";this.checked=false;this.hidden=false;this.disabled=false;this.textContent="";this.dataset={};this.listeners={};this.classList={toggle(){},remove(){},add(){}};}
  append(...children){for(const child of children){this.children.push(child);child.parent=this;}}
  replaceChildren(...children){this.children=[];this.append(...children);}
  setAttribute(key,value){this[key]=value;}
  addEventListener(key,value){this.listeners[key]=value;}
  closest(selector){for(let item=this;item;item=item.parent){if(selector?.startsWith(".")?(item.className||"").split(" ").includes(selector.slice(1)):item.tagName===selector)return item;}return get("label-"+this.id);}
  focus(){} before(){} scrollIntoView(){}
}
function get(id){const existing=created.findLast(el=>el.id===id);if(existing)return existing;if(!elements.has(id)){const el=new Element("div");el.id=id;elements.set(id,el);}return elements.get(id);}
function descendants(el){return el.children.flatMap(child=>[child,...descendants(child)]);}
function text(el){return [el.textContent,...el.children.map(text)].join(" ");}
const documentBody=new Element("body");
get("confirmation-form").append(get("contract-fields"),get("manual-fields"),get("material-review"),get("comparison-identity-fields"));
const identities={employee_name:"가온",employee_id:"100",employer_name:"가온식당"};
const sources=[
 {case_id:"contract-case",document_id:"contract-pdf",document_type:"contract",title:"다른 근로자의 월급 계약서",has_extraction:true,contract_start:"2025-01-01",contract_end:"2027-12-31",identity:{employee_name:"나래",employer_name:"나래회사"},summary:{contract_rate:"3000000",contract_unit:"KRW/month"},review_needed_count:0},
 {case_id:"work-case",document_id:"work-pdf",document_type:"attendance",title:"가온 근무기록",has_extraction:true,identity:identities,period_start:"2026-10-01",period_end:"2026-10-31",summary:{worked_minutes:"9600",worked_time_kind:"actual"},review_needed_count:0},
 {case_id:"slip-case",document_id:"slip-pdf",document_type:"payslip",title:"가온 급여명세서",has_extraction:true,identity:identities,period_start:"2026-10-01",period_end:"2026-10-31",summary:{slip_rate:"10000",slip_unit:"KRW/hour",slip_minutes:"9600",slip_time_kind:"actual",slip_paid_minutes:"12480",gross:"2080000",net:"2000000"},review_needed_count:1},
 {case_id:"bank-case",document_id:"bank-pdf",document_type:"deposit",title:"가온 입금내역",has_extraction:true,identity:identities,period_start:"2026-11-01",period_end:"2026-11-30",summary:{deposit_total:"2000000"},review_needed_count:0},
 {case_id:"unready",document_id:"unready-pdf",document_type:"auto",title:"AI 전 문서",has_extraction:false}
];
const documents=sources.map(source=>({id:source.document_id,title:source.title,kind:"upload",pages:1}));
const emptyDraft=()=>({values:{},manual:{},contract_unit:"KRW/hour",slip_unit:"",time_kind:"",worked_time_kind:"",slip_time_kind:"",period_start:"",period_end:"",material_review:{},source_identity:{},subjects_confirmed:false});
const documentRecord={id:"document-case",document_id:"slip-pdf",document_type:"payslip",jobs:[],draft:emptyDraft(),ocr:{pages:["synthetic source"]},extraction:{fields:[],evidence_catalog:[],verification_method:"ocr_unverified"}};
let record=null,sequence=0;
function createRecord(body){
 const ids=new Set(Object.values(body.selection).flat().filter(Boolean)),selected=sources.filter(source=>ids.has(source.case_id));
 const draft=emptyDraft();Object.assign(draft,{manual:{worked_minutes:"9600",slip_minutes:"9600",slip_rate:"10000",gross:"2080000",net:"2000000"},slip_unit:"KRW/hour",period_start:"2026-10-01",period_end:"2026-10-31",worked_time_kind:"actual",slip_time_kind:"actual"});
 return {id:"comparison-"+(++sequence),kind:"comparison",title:body.title,document_id:null,document_type:"comparison",jobs:[],draft,dirty:true,confirmed_revision:null,revision:1,materials:{selection:body.selection,sources:selected,earnings:[],deductions:[],transactions:[],field_validation:{},field_evidence:{},evidence_catalog:[],warnings:[],suggested:{}},compatibility:{blocked:false,issues:[]}};
}
const context=vm.createContext({structuredClone,document:{body:documentBody,getElementById:get,createElement:tag=>{const el=new Element(tag);created.push(el);return el;},querySelectorAll:selector=>selector==="[data-route]"?[documentBody]:selector.startsWith("#confirmation-form")?descendants(get("confirmation-form")).filter(el=>["input","select"].includes(el.tagName)):[]},
 fetch:async(url,options)=>{const body=options.body?JSON.parse(options.body):null;requests.push({url,method:options.method,body});let response;
  if(url==="/api/extraction-profiles")response=profiles;
  else if(url==="/api/health")response={model:"fixture"};
  else if(url==="/api/documents")response={documents};
  else if(url==="/api/comparison-sources")response={sources};
  else if(url==="/api/comparisons"&&options.method==="GET")response={comparisons:record?[record]:[]};
  else if(url==="/api/comparisons"&&options.method==="POST")response=record=createRecord(body);
  else if(record&&url==="/api/comparisons/"+record.id&&options.method==="GET")response=record;
  else if(record&&url==="/api/comparisons/"+record.id+"/draft"){record.draft=body;record.dirty=true;response=record;}
  else if(record&&url==="/api/comparisons/"+record.id+"/confirm"){assert.equal(record.draft.subjects_confirmed,true);record.confirmed_revision=2;record.dirty=false;response=record;}
  else if(record&&url==="/api/comparisons/"+record.id+"/jobs"){assert.equal(body.stage,"analyze");record.jobs.push("comparison-job");response={id:"comparison-job",stage:"analyze",status:"queued"};}
  else if(url==="/api/jobs/comparison-job")response={id:"comparison-job",stage:"analyze",status:"completed",revision:2};
  else throw new Error("Unexpected API "+options.method+" "+url);
  return {ok:true,json:async()=>structuredClone(response)};},
 localStorage:{getItem:key=>storage.get(key)||null,setItem:(key,value)=>storage.set(key,value),removeItem:key=>storage.delete(key)},location:{search:"",pathname:"/compare"},history:{replaceState(){},pushState(){}},window:{addEventListener(){}},URLSearchParams,setTimeout(){},clearTimeout(){}
});
for(const file of ["results.js","compare.js","pages.js"])vm.runInContext(fs.readFileSync(path.join(__dirname,"../../dev/web",file),"utf8"),context);
let app=fs.readFileSync(path.join(__dirname,"../../dev/web/app.js"),"utf8").replace(/initialize\(\)\.catch\(error=>\{[^\n]+\}\);\s*$/,"globalThis.uiInitialized=initialize();");vm.runInContext(app,context);
const run=code=>vm.runInContext(code,context);
function assertCompareButtonsDisabled(disabled,message){for(const id of ["confirm-compare","confirm-compare-top"])assert.equal(get(id).disabled,disabled,id+": "+message);}
function assertCompareActionReason(reason){for(const id of ["comparison-action-status","comparison-action-status-top"])assert.ok(text(get(id)).includes(reason),id+" explains "+reason);}
(async()=>{
 await context.uiInitialized;
 assert.equal(documentBody.listeners.click,undefined,"Route handlers belong to navigation anchors; body clicks must retain checkbox defaults.");
 assert.equal(run("current"),null,"The compare route starts without adopting an arbitrary document case.");
 assert.equal(get("comparison-cards").children.length,4,"Only AI-complete documents are selectable.");
 assert.ok(!text(get("comparison-cards")).includes("AI 전 문서"));
 assert.ok(text(get("comparison-cards")).includes("사용자 확인 전"));assert.ok(!text(get("comparison-cards")).includes("사용자 확인 완료"));
 assert.ok(text(get("comparison-cards")).includes("2025-01-01"),"Contracts display their effective period.");
 assert.equal(requests.some(item=>item.method==="POST"),false,"Opening the workspace creates no cases or jobs.");
 context.sources=sources;context.documentFixture=structuredClone(documentRecord);run("documentCase=documentFixture;for(const source of sources.slice(0,4))selectComparisonSource(source,true)");
 assert.ok(text(get("comparison-selection-warnings")).includes("근로자 이름이 다른"));assert.ok(text(get("comparison-selection-warnings")).includes("임금 단위가 다릅니다"));
 assert.ok(!text(get("comparison-selection-warnings")).includes("대상 기간이 다릅니다"),"Bank query periods do not imply a conflicting payroll period.");
 run("selectComparisonSource(sources[0],false)");get("comparison-title").value="가온 10월 비교";await run("applyComparisonSelection()");
 assert.equal(record.kind,"comparison");assert.equal(record.document_id,null);assert.equal(record.title,"가온 10월 비교");
 assert.deepEqual(record.materials.selection,{contract_case_id:null,attendance_case_ids:["work-case"],payslip_case_id:"slip-case",deposit_case_ids:["bank-case"]});
 assert.equal(storage.get("agent-last-comparison"),record.id);assert.equal(storage.has("agent-case-null"),false);
 assert.ok(run("routeUrl('compare')").includes("?comparison="));assert.ok(run("routeUrl('ai')").includes("?case=document-case"));
 assert.equal(get("worked-time-kind").value,"actual");assert.equal(get("slip-time-kind").value,"actual");assert.ok(text(get("slip-paid-reference")).includes("208시간"));assert.equal(get("manual-slip_minutes").value,"9600","A 208-hour paid-time reference never replaces the actual 160-hour comparison value.");
 run("comparisonSources.find(source=>source.case_id==='slip-case').summary.slip_paid_minutes='18000';renderComparisonContext()");assert.ok(text(get("slip-paid-reference")).includes("208시간"),"Saved source context precedes new extraction candidates.");
 assertCompareButtonsDisabled(true,"AI completion never substitutes for subject confirmation.");assertCompareActionReason("같은 근로자");
 run("current.draft.source_identity={'bank-case':{employee_name:'',employee_id:'',employer_name:'수기 사업장'}};renderIdentityInputs()");assert.equal(run("identityInputs.get('bank-case').employee_name.value"),"가온","Partial overrides preserve source values in other identity fields.");assert.ok(text(get("comparison-identity-fields")).includes("원문 후보: 가온식당"));run("current.draft.source_identity={};renderIdentityInputs()");
 run("identityInputs.get('bank-case').employee_name.value='다른 사람';identityInputs.get('bank-case').employer_name.value='다른 사업장';$('same-subject-confirmed').checked=true;changed()");assertCompareButtonsDisabled(true,"Cross-worker combinations remain blocked even if the acknowledgment is checked.");assertCompareActionReason("근로자 이름");assertCompareActionReason("사업장 이름");
 assert.equal(requests.some(item=>item.url.endsWith("/confirm")||item.url.endsWith("/jobs")),false,"Acknowledging identity conflicts never starts comparison.");
 run("identityInputs.get('bank-case').employee_name.value='가온';$('same-subject-confirmed').checked=true;changed()");assertCompareButtonsDisabled(true,"A workplace mismatch still blocks both actions after the worker name is resolved.");assertCompareActionReason("사업장 이름");
 run("identityInputs.get('bank-case').employer_name.value='가온식당';$('same-subject-confirmed').checked=true;changed()");await run("flushDraft()");assertCompareButtonsDisabled(false,"Resolving conflicts enables both comparison actions.");
 run("identityInputs.get('bank-case').employee_name.value='가 온';identityInputs.get('bank-case').employer_name.value='가온 식당';$('same-subject-confirmed').checked=true;changed()");assertCompareButtonsDisabled(false,"Whitespace does not create an identity mismatch.");assert.ok(!text(get("comparison-identity-warnings")).includes("일치하지 않습니다"),"Identity whitespace uses server-compatible normalization.");run("identityInputs.get('bank-case').employee_name.value='가온';changed()");await run("flushDraft()");
 const draftRequest=requests.filter(item=>item.url.endsWith("/draft")).at(-1);assert.equal(draftRequest.body.worked_time_kind,"actual");assert.equal(draftRequest.body.slip_time_kind,"actual");assert.equal(draftRequest.body.time_kind,"");assert.deepEqual(draftRequest.body.source_identity,{},"Source defaults must not become persistent overrides after refresh.");
 run("identityInputs.get('bank-case').employee_id.value='101'");assert.equal(run("readSourceIdentity()['bank-case'].employee_id"),"101");run("identityInputs.get('bank-case').employee_id.value='100'");
 run("$('worked-time-kind').value='';changed()");assertCompareButtonsDisabled(true,"Missing time meaning blocks both actions.");assert.ok(text(get("time-compatibility")).includes("입력한 시간마다"));run("$('worked-time-kind').value='actual';changed()");
 run("$('slip-time-kind').value='paid';changed()");assert.ok(text(get("time-compatibility")).includes("서로 다른 기준"));run("$('slip-time-kind').value='actual';changed()");assertCompareButtonsDisabled(false,"Both actions become available after time meaning is provided.");
 await Promise.all([get("confirm-compare-top").listeners.click(),get("confirmation-form").listeners.submit({preventDefault(){}})]);await run("pollJob()");
 assert.equal(requests.filter(item=>item.url.endsWith("/confirm")).length,1);assert.equal(requests.filter(item=>item.url.endsWith("/jobs")).length,1);assert.equal(requests.find(item=>item.url.endsWith("/jobs")).body.stage,"analyze");
 await run("navigateRoute('ai')");assert.equal(run("current.id"),"document-case");assert.deepEqual(JSON.parse(run("JSON.stringify(current.draft)")),documentRecord.draft,"Comparison edits never alter the original document draft.");
 await run("navigateRoute('compare')");assert.equal(run("current.id"),record.id);
 await run("openComparison(null)");assert.equal(run("current"),null);await run("openComparison('"+record.id+"')");assert.equal(get("comparison-title").value,"가온 10월 비교");
 assert.equal(requests.some(item=>item.url.startsWith("/api/cases/")&&item.method!=="GET"),false,"Comparison creation, edits, confirmation and analysis never mutate source cases.");
 assert.equal(requests.some(item=>["extract","ocr"].includes(item.body?.stage)),false,"Comparisons never trigger paid extraction or OCR.");
 console.log("Independent comparison cards, identity gates, time meanings, single action, restoration and document isolation verified.");
})().catch(error=>{console.error(error);process.exitCode=1;});
