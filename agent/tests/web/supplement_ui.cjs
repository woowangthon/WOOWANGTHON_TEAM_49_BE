const assert=require("node:assert/strict"),fs=require("node:fs"),path=require("node:path"),vm=require("node:vm");
const profiles=JSON.parse(process.argv[2]),elements=new Map(),created=[],requests=[];
class Element{
 constructor(tag){this.tagName=tag;this.children=[];this.value="";this.checked=false;this.disabled=false;this.hidden=false;this.dataset={};this.listeners={};this.textContent="";this.classList={toggle(){},remove(){},add(){}};}
 append(...items){for(const item of items){this.children.push(item);item.parent=this;}}
 prepend(item){this.children.unshift(item);item.parent=this;}
 replaceChildren(...items){this.children=[];this.append(...items);}
 setAttribute(key,value){this[key]=value;}
 addEventListener(key,handler){this.listeners[key]=handler;}
 closest(selector){for(let el=this;el;el=el.parent){if(selector.startsWith(".")?(el.className||"").split(" ").includes(selector.slice(1)):el.tagName===selector)return el;}return get("label-"+this.id);}
 focus(){} before(){} scrollIntoView(){}
}
function get(id){const found=created.findLast(el=>el.id===id);if(found)return found;if(!elements.has(id)){const el=new Element("div");el.id=id;elements.set(id,el);}return elements.get(id);}
const descendants=el=>el.children.flatMap(child=>[child,...descendants(child)]);
const text=el=>[el.textContent,...el.children.map(text)].join(" ");
const button=(el,label)=>descendants(el).find(child=>child.tagName==="button"&&child.textContent===label);
const all=()=>[...elements.values()].flatMap(el=>[el,...descendants(el)]);
get("confirmation-form").append(get("contract-fields"),get("manual-fields"));
const field=(id,value,status="valid",page=1)=>({field_id:id,raw_value:value,suggested_value:value,normalized_value:status==="valid"?value:null,validation_status:status,validation_reasons:status==="needs_review"?["SOURCE_VALUE_MISMATCH"]:[],unit:id.includes("end")?"date":"KRW/month",source_quote:value?"인용 <script>"+value:"",source_page:page,evidence_refs:[],issues:[]});
const A={fields:[field("contract_rate","3000000"),field("contract_end",null,"missing"),field("payday","25","needs_review")],evidence_catalog:[],issues:[],page_count:2,verification_method:"ocr_unverified"};
let record={id:"case-supplement",document_id:"pdf-test",document_type:"contract",extraction_revision:3,extraction_a_revision:3,jobs:[],ocr:{pages:["합성 원문",""]},extraction:structuredClone(A),extraction_a:structuredClone(A),supplements:[],candidate_selections:{},draft:{values:{contract_rate:"3000000"},manual:{},contract_unit:"KRW/month",slip_unit:"",period_start:"",period_end:"",time_kind:"",material_review:{}},dirty:true};
let job={id:"job-supplement",stage:"supplement",status:"running",document_type:"contract",revision:3,events:[],usage:{total_tokens:0}};
const responses={"/api/extraction-profiles":profiles,"/api/health":{model:"fixture"},"/api/documents":{documents:[{id:"pdf-test",title:"합성 계약서.pdf",pages:2,kind:"upload"}]},"/api/comparison-sources":{sources:[]}};
const context=vm.createContext({structuredClone,document:{body:{dataset:{}},getElementById:get,createElement:tag=>{const el=new Element(tag);created.push(el);return el;},querySelectorAll:selector=>selector==="[data-supplement-field]"?all().filter(el=>el.dataset.supplementField):selector==="[data-candidate-action]"?all().filter(el=>el.dataset.candidateAction):selector.startsWith("#confirmation-form")?descendants(get("confirmation-form")).filter(el=>["input","select"].includes(el.tagName)):[]},
 fetch:async(url,options)=>{const body=options.body?JSON.parse(options.body):null;requests.push({url,method:options.method,body});let result;
  if(url in responses)result=responses[url];
  else if(url==="/api/cases/case-supplement")result=record;
  else if(url==="/api/cases/case-supplement/jobs"){assert.deepEqual(body,{stage:"supplement",field_ids:["contract_end"],extraction_revision:3});record.jobs.push(job.id);result=job;}
  else if(url==="/api/jobs/job-supplement")result=job;
  else if(url==="/api/jobs/job-supplement/cancel"){job.status="cancelled";result=job;}
  else if(url==="/api/jobs/job-supplement/resume"){job.status="running";result=job;}
  else if(url==="/api/cases/case-supplement/candidate-selection"){assert.equal(body.source_confirmed,true);assert.equal(body.extraction_revision,record.extraction_revision);record.candidate_selections[body.field_id]={candidate_id:body.candidate_id};record.extraction_revision++;result=record;}
  else throw new Error("Unexpected "+options.method+" "+url);
  return {ok:true,json:async()=>structuredClone(result)};},localStorage:{getItem:key=>key==="agent-last-case"?record.id:null,setItem(){},removeItem(){}},location:{search:"?case=case-supplement",pathname:"/ai"},history:{replaceState(){},pushState(){}},window:{addEventListener(){}},URLSearchParams,setTimeout(){},clearTimeout(){}
});
for(const file of ["results.js","compare.js","pages.js"])vm.runInContext(fs.readFileSync(path.join(__dirname,"../../dev/web",file),"utf8"),context);
vm.runInContext(fs.readFileSync(path.join(__dirname,"../../dev/web/app.js"),"utf8").replace(/initialize\(\)\.catch\(error=>\{[^\n]+\}\);\s*$/,"globalThis.uiInitialized=initialize();"),context);
const run=code=>vm.runInContext(code,context);
(async()=>{
 await context.uiInitialized;
 assert.equal(requests.some(request=>request.method==="POST"),false,"Opening a document never starts B.");
 let checks=descendants(get("document-extraction")).filter(el=>el.dataset.supplementField);assert.deepEqual(checks.map(el=>el.dataset.supplementField).sort(),["contract_end","payday"]);
 const topChecks=descendants(get("supplement-fields")).filter(el=>el.dataset.supplementField);
 assert.deepEqual(topChecks.map(el=>el.dataset.supplementField).sort(),["contract_end","payday"],"The supplement panel lists both missing and review-only fields without requiring a scroll through the document summary.");
 assert.equal(get("supplement-empty").hidden,true);
 assert.ok(text(get("supplement-fields")).includes("계약 종료일"));assert.ok(text(get("supplement-fields")).includes("지급일"));
 assert.equal(get("supplement").disabled,true);await get("supplement").listeners.click();assert.equal(requests.some(request=>request.method==="POST"),false,"An empty selection cannot start a paid request.");
 const end=checks.find(el=>el.dataset.supplementField==="contract_end"),topEnd=topChecks.find(el=>el.dataset.supplementField==="contract_end");
 topEnd.checked=true;topEnd.listeners.change();assert.equal(end.checked,true,"Selecting the upper field synchronizes its lower checkbox.");assert.equal(get("supplement").disabled,false);
 end.checked=false;end.listeners.change();assert.equal(topEnd.checked,false,"Clearing the lower field synchronizes its upper checkbox.");assert.equal(get("supplement").disabled,true);
 end.checked=true;end.listeners.change();assert.equal(topEnd.checked,true);assert.equal(run("supplementSelection.size"),1,"The same field shown twice counts only once.");assert.equal(get("supplement").disabled,false);
 await get("supplement").listeners.click();assert.equal(requests.filter(request=>request.url.endsWith("/jobs")&&request.method==="POST").length,1);assert.equal(get("extraction-cancel").hidden,false);assert.ok(get("extraction-job-status").textContent.includes("PDF 보완 읽기"));
 await get("extraction-cancel").listeners.click();assert.equal(get("extraction-resume").hidden,false);assert.equal(job.status,"cancelled");
 await get("extraction-resume").listeners.click();assert.equal(job.status,"running");
 job.status="failed";job.error="합성 보완 실패";await run("pollJob()");assert.equal(get("extraction-resume").hidden,false);assert.ok(get("development-details").textContent.includes("합성 보완 실패"));assert.equal(requests.filter(request=>request.url.endsWith("/jobs")&&request.method==="POST").length,1,"Failure never automatically creates a new paid request.");job.error=null;
 const B={...structuredClone(A),fields:[field("contract_end","2027-10-31"),field("payday","30"),field("contract_rate","3000000")]};record.supplements=[{id:"supplement-one",base_extraction_revision:3,payload:B}];job.status="completed";await run("pollJob()");
 assert.deepEqual(record.extraction,A,"B completion cannot overwrite A/effective values.");assert.deepEqual(record.draft.values,{contract_rate:"3000000"},"B completion preserves confirmed user input.");
 assert.equal(requests.some(request=>request.url.endsWith("/candidate-selection")),false,"Neither agreement nor B completion automatically selects a candidate.");
 const rendered=text(get("document-extraction"));for(const label of ["A·B 값 일치","A·B 값 다름","한쪽 값 없음","원문 확인 필요","기본 읽기 A","보완 읽기 B","인용 <script>"])assert.ok(rendered.includes(label),label);
 assert.equal(descendants(get("document-extraction")).some(el=>el.tagName==="script"),false);
 let comparisons=descendants(get("document-extraction")).filter(el=>el.className==="candidate-comparison"),date=comparisons.find(el=>text(el).includes("2027-10-31"));assert.equal(button(date,"원문 확인 후 B값 사용").disabled,false);
 button(date,"원문 보기 ↗").listeners.click();assert.ok(get("pdf").src.endsWith("#page=1"));
 await button(date,"원문 확인 후 B값 사용").listeners.click();let selection=requests.filter(request=>request.url.endsWith("/candidate-selection")).at(-1);assert.equal(selection.body.candidate_id,"supplement-one:contract_end");assert.equal(selection.body.extraction_revision,3);
 assert.equal(run("current.extraction_a_revision"),3);assert.equal(run("current.extraction_revision"),4);assert.equal(button(descendants(get("document-extraction")).find(el=>el.className==="candidate-comparison"&&text(el).includes("2027-10-31")),"원문 확인 후 B값 사용").disabled,false,"A lineage keeps B available after another field selection.");
 const manual=descendants(get("document-extraction")).filter(el=>el.className==="candidate-manual").find(el=>descendants(el).some(input=>input["aria-label"]==="계약 종료일 직접 확인한 값"));const input=descendants(manual).find(el=>el["aria-label"]==="계약 종료일 직접 확인한 값");assert.equal(input.value,"","Manual correction starts empty, never with an uncertain proposal.");input.value="2028-01-01";input.listeners.input();await button(manual,"원문 확인 후 직접 입력값 사용").listeners.click();selection=requests.filter(request=>request.url.endsWith("/candidate-selection")).at(-1);assert.equal(selection.body.candidate_id,"manual");assert.equal(selection.body.value,"2028-01-01");assert.equal(selection.body.unit,"date");
 const before=requests.length;button(get("document-extraction"),"이번 선택 보류").listeners.click();assert.equal(requests.length,before,"Deferring does not initiate a model or choose a value.");
 run("current.supplements[0].payload.fields[0].source_page=99;renderDocumentExtraction(current.extraction,current.document_type)");date=descendants(get("document-extraction")).find(el=>el.className==="candidate-comparison"&&text(el).includes("2027-10-31"));assert.equal(button(date,"원문 확인 후 B값 사용").disabled,true,"A B candidate with invalid source page cannot be used.");
 run("current.supplements[0].base_extraction_revision=2;renderDocumentExtraction(current.extraction,current.document_type)");assert.ok(text(get("document-extraction")).includes("이전 기본 읽기에 대한 보완 결과"));
 run("current.supplements[0].payload.fields[0].validation_status='needs_review';current.supplements[0].payload.fields[0].validation_reasons=['SUPPLEMENT_ROW_UNCERTAIN'];renderDocumentExtraction(current.extraction,current.document_type)");assert.ok(text(get("document-extraction")).includes("원문의 어느 행"));assert.ok(!text(get("document-extraction")).includes("SUPPLEMENT_ROW_UNCERTAIN"));
 const requestsBeforeEligibilityChecks=requests.length;
 run("current.extraction.fields=current.extraction.fields.map(field=>({...field,normalized_value:'25',validation_status:'valid'}));renderDocumentExtraction(current.extraction,current.document_type)");
 assert.equal(descendants(get("supplement-fields")).filter(el=>el.dataset.supplementField).length,0,"Already reviewed effective values are absent from the supplement picker.");
 assert.equal(get("supplement-empty").hidden,false);assert.ok(text(get("supplement-empty")).trim());assert.equal(get("supplement").disabled,true);assert.equal(run("supplementSelection.size"),0,"Resolved fields cannot remain selected invisibly.");
 run("current.extraction=structuredClone(current.extraction_a);for(const field of current.extraction.fields){if(field.validation_status!=='valid'){field.raw_value=null;field.suggested_value=null;field.normalized_value=null;}}renderDocumentExtraction(current.extraction,current.document_type)");
 assert.deepEqual(descendants(get("supplement-fields")).filter(el=>el.dataset.supplementField).map(el=>el.dataset.supplementField).sort(),["contract_end","payday"],"A missing AI suggestion must not prevent explicitly selecting a field for PDF rereading.");
 const pendingCheck=descendants(get("supplement-fields")).find(el=>el.dataset.supplementField==="payday");pendingCheck.checked=true;pendingCheck.listeners.change();
 run("current.extraction_needs_review=true;renderDocumentExtraction(current.extraction,current.document_type)");assert.equal(get("supplement").disabled,true,"An incompatible base extraction cannot start supplementation.");
 assert.equal(descendants(get("supplement-fields")).filter(el=>el.dataset.supplementField).length,0);assert.equal(run("supplementSelection.size"),0);assert.equal(get("supplement-empty").hidden,false);assert.ok(text(get("supplement-empty")).includes("기본 항목 정리를 다시 완료"));
 run("current.extraction_needs_review=false;current.extraction=null;renderDocumentExtraction(null,current.document_type)");assert.equal(get("supplement-panel").hidden,true);assert.equal(get("supplement").disabled,true);
 assert.equal(requests.length,requestsBeforeEligibilityChecks,"Rendering unavailable, resolved or review-only fields never starts an AI request.");
 console.log("Visible supplement selection, synchronized controls, explicit PDF requests, cancellation/resume, A/B isolation and candidate review verified.");
})().catch(error=>{console.error(error);process.exitCode=1;});
