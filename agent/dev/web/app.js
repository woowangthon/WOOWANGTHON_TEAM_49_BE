"use strict";
const $ = id => document.getElementById(id);
const labels = {pay_type:"임금 형태",contract_rate:"임금 금액",contract_start:"계약 시작일",contract_end:"계약 종료일",work_start:"출근 시각",work_end:"퇴근 시각",break_minutes:"휴게 시간 (분)",scheduled_daily_minutes:"일 소정근로시간 (분)",scheduled_weekly_minutes:"주 소정근로시간 (분)",payday:"지급일"};
const manualLabels = {slip_rate:"명세서 단가 (선택 단위)",worked_minutes:"근태 시간 (분)",slip_minutes:"명세서 시간 (분)",gross:"명세서 총지급액 (원)",deduction:"명세서 총공제액 (원)",net:"명세서 실수령액 (원)",deposit:"연결된 입금액 (원)",earning_item:"지급 항목 합계 (원)",deduction_item:"공제 항목 합계 (원)"};
const comparisonSides = {"R-01":["명세서 단가","계약서 단가"],"R-02":["명세서 시간","근무기록 시간"],"R-03":["확인한 지급 항목 합계","명세서 총지급액"],"R-04":["확인한 공제 항목 합계","명세서 총공제액"],"R-05":["총지급액 − 총공제액","명세서 실수령액"],"R-06":["명세서 실수령액","선택·배분한 입금액"]};
comparisonSides["R-07"]=["계산한 임금","같은 항목의 명세서 금액"];
const stageLabels = {ocr:"원문 읽기",extract:"AI 항목 정리",supplement:"PDF 보완 읽기",analyze:"대조",explain:"설명",report:"보고서"};
const sourceConversions = new Set(["SOURCE_UNIT_FROM_HEADER","SOURCE_TEXT_SEPARATOR_NORMALIZED","SOURCE_SCALAR_SPELLING_RESTORED"]);
function reviewIssues(field) { return (field.issues||[]).filter(issue=>!sourceConversions.has(issue)); }
const validationLabels={valid:"검사 통과 · 사용자 확인 전",needs_review:"확인 필요",missing:"미기입"};
const validationReasonLabels={SOURCE_QUOTE_MISMATCH:"인용문을 저장된 원문에서 확인하지 못했습니다.",SOURCE_VALUE_MISMATCH:"AI가 읽은 값과 저장된 원문의 표기가 일치하지 않습니다.",SOURCE_NORMALIZATION_INVALID:"원문을 숫자·날짜·시간으로 바꾸는 과정에서 오류가 발견되었습니다.",SOURCE_UNIT_MISMATCH:"단위를 원문에서 확인하지 못했습니다.",SOURCE_PAGE_INVALID:"AI가 제시한 페이지를 원본에서 확인할 수 없습니다.",INVALID_PAGE:"AI가 제시한 페이지를 원본에서 확인할 수 없습니다.",INVALID_UNIT:"항목의 단위를 확인해 주세요.",UNIT_MISMATCH:"항목의 단위를 확인해 주세요.",INVALID_NUMBER:"숫자 형식을 확인해 주세요.",INVALID_DATE:"날짜 형식을 확인해 주세요.",INVALID_TIME:"시간 형식을 확인해 주세요.",OCR_LOW_CONFIDENCE:"문자 인식 신뢰도가 낮습니다. 원본 PDF를 확인해 주세요."};
Object.assign(validationReasonLabels,{SOURCE_VALUE_MISSING:"원문 값 없이 AI 제안만 있어 확인이 필요합니다.",REQUIRED_VALUE_MISSING:"대조에 필요한 값이 없습니다. 원문에서 확인해 주세요.",NORMALIZED_VALUE_UNAVAILABLE:"원문 값을 대조에 사용할 숫자·날짜·시간으로 바꾸지 못했습니다.",DUPLICATE_FIELD_ID:"같은 항목이 중복되어 사용할 값을 확인해야 합니다.",TABLE_SOURCE_MISMATCH:"표의 인용문을 OCR 원문에서 확인하지 못했습니다.",TABLE_ROW_SOURCE_REUSED:"서로 다른 행이 같은 원문을 가리킵니다. 행 구분을 확인해 주세요.",TABLE_CELL_SOURCE_MISMATCH:"이 셀의 값을 해당 표 행의 원문에서 확인하지 못했습니다.",TABLE_SHAPE_INVALID:"표의 열 수와 항목 구성이 맞지 않습니다. 원문의 행을 확인해 주세요."});
Object.assign(validationReasonLabels,{CANDIDATE_RANGE_INVALID:"이 항목에 사용할 수 있는 숫자 또는 길이 범위를 벗어났습니다.",PERIOD_CONFLICT:"시작일이 종료일보다 늦습니다. 원문에서 기간을 확인해 주세요.",UNKNOWN_FIELD:"이 문서 유형에 맞지 않는 항목입니다. 원문과 문서 유형을 확인해 주세요."});
Object.assign(validationReasonLabels,{USER_CONFIRMED_SELECTION:"원문을 확인한 사용자가 선택한 값입니다.",PDF_VISUAL_REQUIRES_USER_CONFIRMATION:"PDF를 읽은 보완 후보입니다. 원문에서 직접 확인해 주세요.",PDF_REQUIRES_USER_CONFIRMATION:"PDF를 읽은 보완 후보입니다. 원문에서 직접 확인해 주세요.",PDF_SUPPLEMENT_VALUE_UNAVAILABLE:"원본 PDF에서도 이 값을 확인하지 못했습니다. 직접 입력하거나 보류해 주세요.",SUPPLEMENT_ROW_UNCERTAIN:"보완한 값이 원문의 어느 행에 해당하는지 확실하지 않습니다. 행의 날짜·항목명을 확인해 직접 입력하거나 보류하세요."});
function fieldValidationStatus(field) {
  if(["valid","needs_review","missing"].includes(field?.validation_status))return field.validation_status;
  return field?.normalized_value!=null?"valid":field?.raw_value!=null||field?.suggested_value!=null?"needs_review":"missing";
}
function validationReasons(field) { return [...new Set([...(field.validation_reasons||[]),...reviewIssues(field)])]; }
function extractionReviewCount(extraction) { return (extraction?.fields||[]).filter(field=>fieldValidationStatus(field)==="needs_review").length; }
function appendValidationDetails(target, field, canEdit=true, pdfCandidate=false) {
  const status=fieldValidationStatus(field),suggestion=Object.hasOwn(field,"suggested_value")?field.suggested_value:field.normalized_value;
  target.append(node("span",field.issues?.includes("USER_CONFIRMED_SELECTION")?"사용자 원문 확인 · 선택됨":validationLabels[status],"validation-status "+status));
  const values=node("dl",undefined,"candidate-source-values");
  for(const [label,value] of [["원문 표기 후보",field.raw_value],["AI 제안",suggestion]])values.append(node("dt",label),node("dd",value==null||value===""?"없음":String(value)));
  target.append(values);
  if(field.source_quote&&!(field.evidence_refs||[]).length){const quote=node("div",undefined,"unverified-quote");quote.append(node("small",pdfCandidate?"AI가 인용한 문장 · 원본 PDF 직접 확인 필요":"AI가 인용한 문장 · OCR 원문 일치 미확인"),node("blockquote",field.source_quote));target.append(quote);}
  for(const reason of validationReasons(field))target.append(node("small",validationReasonLabels[reason]||reason,"validation-reason"));
  if(status==="needs_review")target.append(node("small",canEdit?"AI 제안은 입력값에 반영되지 않습니다. 원문을 확인해 직접 입력하거나 보류하세요.":"이 항목은 원문 확인이 필요합니다. 아래 안내에 따라 행을 보완하거나 보류하세요.","muted"));
}
function appendReviewActions(target, input, onDefer, canEdit=true) {
  if(!input)return;
  const actions=node("div",undefined,"field-review-actions");
  const edit=node("button","직접 입력"),defer=node("button","보류 · 빈값으로 두기");
  for(const button of [edit,defer]){button.type="button";button.disabled=Boolean(current?.materials_invalid||busy());}
  edit.addEventListener("click",()=>input.focus());
  defer.addEventListener("click",()=>{input.value="";if(onDefer)onDefer();changed();notice("이 항목은 미확인으로 보류했습니다. 빈값은 대조에서 정보 부족으로 남습니다.");});
  if(canEdit)actions.append(edit);actions.append(defer);target.append(actions);
}
function renderInputValidation(target, field, input, evidence) {
  if(!field)return;
  const detail=node("div",undefined,"field-review "+fieldValidationStatus(field));appendValidationDetails(detail,field);
  if(evidence)detail.append(evidence);
  if(!(field.evidence_refs||[]).length){const documentId=field.source_document_id||(!current.materials?.sources?.length?current.document_id:null);if(documentId){const source=node("button","원본 PDF 보기 ↗","evidence-link");source.type="button";source.addEventListener("click",()=>openSourceDocument(documentId,field.source_title,1));detail.append(source);}}
  if(fieldValidationStatus(field)!=="valid")appendReviewActions(detail,input);
  target.append(detail);
}
function appendMaterialWarnings(target, item, input, onDefer) {
  const warnings=item.validation_warnings||[];
  if(!warnings.length)return;
  const group=node("div",undefined,"material-validation-warnings");
  for(const field of warnings){const detail=node("div",undefined,"field-review "+fieldValidationStatus(field));detail.append(node("strong",fieldReviewLabel(field)));appendValidationDetails(detail,field,!hasNonAmountWarnings(item));detail.append(materialEvidenceButtons(field.evidence_refs,current.materials.evidence_catalog));
    const documentId=field.source_document_id||current.materials.sources.find(source=>source.case_id===item.source_case_id)?.document_id;
    if(!(field.evidence_refs||[]).length&&documentId){const source=node("button","원본 PDF 보기 ↗","evidence-link");source.type="button";source.addEventListener("click",()=>openSourceDocument(documentId,field.source_title,1));detail.append(source);}group.append(detail);}
  appendReviewActions(group,input,onDefer,!hasNonAmountWarnings(item));const originalSource=current.materials.sources.find(source=>source.case_id===item.source_case_id);if(originalSource)group.append(supplementSourceLink(originalSource));target.append(group);
}
function hasNonAmountWarnings(item) { return item.validation_warnings?.some(field=>!field.field_id?.endsWith("_amount")); }
function fieldReviewLabel(field) {
  if(field.label)return field.label;
  if(labels[field.field_id]||manualLabels[field.field_id])return labels[field.field_id]||manualLabels[field.field_id];
  const match=/^([a-z]+)_[1-9]\d{0,3}_([a-z_]+)$/.exec(field.field_id||"");
  for(const profile of Object.values(profiles)){const definition=match?profile.rows?.[match[1]]?.fields?.[match[2]]:profile.fields?.[field.field_id];if(definition)return definition.label;}
  return "항목 확인";
}
let docs = [], profiles = {}, current = null, currentJob = null, pollTimer, draftTimer, pendingDraft = false, changingType = false, saveChain = Promise.resolve();
let supplementSelection=new Set(),supplementSelectionKey="",candidateRequestPending=false;
const routeInfo = {
ocr:{title:"문서 등록·읽기",description:"PDF의 텍스트를 먼저 읽고, 스캔된 페이지는 OCR로 읽습니다.",eyebrow:"문서 준비",step:1},
ai:{title:"AI 정리",description:"저장한 원문으로 항목을 정리하고, 필요한 항목만 PDF로 보완합니다.",eyebrow:"문서 준비",step:2},
compare:{title:"계산·대조",description:"이번 급여에 사용할 자료와 조건을 확인하세요.",eyebrow:"급여 확인",step:3},
results:{title:"결과",description:"계산된 범위와 자료의 차이를 근거와 함께 확인하세요.",eyebrow:"급여 확인",step:4},
questions:{title:"확인 질문",description:"부족한 정보와 필요한 날짜를 확인하고 입력을 보완하세요.",eyebrow:"급여 확인",step:5},
inquiry:{title:"회사 문의",description:"확인된 내용으로 문의 초안을 만들고 직접 수정하세요.",eyebrow:"급여 확인",step:6},
reports:{title:"자료 받기",description:"보고서와 선택한 원본을 같은 결과 버전으로 받으세요.",eyebrow:"급여 확인",step:7},
glossary:{title:"쉬운 용어",description:"급여 문서에 나오는 말을 쉬운 설명과 예시로 알아보세요.",eyebrow:"이용 도움",step:8}};
function payrollRoute(route) {return ["compare","results","questions","inquiry","reports"].includes(route);}
let routeResultId=new URLSearchParams(location.search).get("result")||"";
let routeComparisonId=new URLSearchParams(location.search).get("comparison")||"";
let activeRoute = Object.hasOwn(routeInfo,location.pathname.split("/")[1])?location.pathname.split("/")[1]:"ocr";
let comparisonSources = null, comparisonSourcesRequest = null, loadingMaterials = false;
let materialSelectors = {}, materialInputs = {earnings:new Map(),deductions:new Map(),transactions:new Map(),row_roles:new Map(),manual_rows:new Map()};
let distinctTransactionsInput = null;
let manualRowSequence = 0;
const statementRoleLabels = {detail:"세부항목 · 합계에 포함",subtotal:"소계 · 합계에서 제외",total:"총계 · 합계에서 제외",excluded:"대조에서 제외"};
function node(tag, text, className) { const el=document.createElement(tag); if(text!==undefined) el.textContent=text; if(className) el.className=className; return el; }
function notice(message, error=false) { $("notice").textContent=message; $("notice").className="notice"+(error?" error":""); $("notice").hidden=!message; }
async function api(path, method="GET", body) {
  const headers={"X-Local-Agent":"1"};
  if(body!==undefined) headers["Content-Type"]="application/json";
  const response=await fetch(path,{method,headers,body:body===undefined?undefined:JSON.stringify(body)});
  const result=await response.json(); if(!response.ok) throw new Error(result.error||"요청을 처리하지 못했습니다."); return result;
}
function safeAction(fn) { return async event => { try { await fn(event); } catch(error) { notice(error.message,true); } }; }
function busy() { return currentJob && ["queued","running"].includes(currentJob.status); }
function docForCase() { return docs.find(d=>d.id===current?.document_id); }
function routeUrl(route) {
  const params=new URLSearchParams(),comparison=comparisonCase||(isComparison()?current:null),doc=documentCase||(!isComparison()?current:null);
  if(doc&&(!payrollRoute(route)||!comparison&&!routeComparisonId))params.set("case",doc.id);
  if(comparison)params.set("comparison",comparison.id);else if(routeComparisonId)params.set("comparison",routeComparisonId);
  if(routeResultId)params.set("result",routeResultId);
  if(route==="glossary"){const currentParams=new URLSearchParams(location.search),back=activeRoute==="glossary"?currentParams.get("return"):routeUrl(activeRoute)+(location.hash||"");if(back)params.set("return",back);if(activeRoute==="glossary"&&currentParams.get("term"))params.set("term",currentParams.get("term"));}
  return "/"+route+(params.toString()?"?"+params.toString():"");
}
function rememberCase() { if(isComparison())return rememberComparison();documentCase=current;localStorage.setItem("agent-last-case",current.id);localStorage.setItem("agent-case-"+current.document_id,current.id);history.replaceState(null,"",routeUrl(activeRoute));updateRouteLinks(); }
function number(value) { return value===null||value===undefined||String(value).trim()===""?"미확인":Number.isFinite(Number(value))?Number(value).toLocaleString("ko-KR"):String(value); }
function buildFields() {
  for(const [key,label] of Object.entries(labels)) {
    const wrap=node("label",undefined,"field"); wrap.htmlFor="value-"+key; wrap.append(node("span",label));
    const input=node("input"); input.id="value-"+key; input.placeholder="미확인";input.setAttribute("aria-label",label);
    input.type=key.startsWith("contract_")&&["contract_start","contract_end"].includes(key)?"date":"text";
    if(key==="contract_rate"||key.includes("minutes")) input.inputMode="decimal";
    wrap.append(input); const meta=node("div",undefined,"field-meta"); meta.id="meta-"+key; wrap.append(meta); $("contract-fields").append(wrap);
  }
  for(const [key,label] of Object.entries(manualLabels)) {
    const wrap=node("label",label,"field"); wrap.htmlFor="manual-"+key;
    const input=node("input"); input.id="manual-"+key; input.placeholder="미확인"; input.inputMode="decimal";input.setAttribute("aria-label",label);
    wrap.append(input);if(typeof glossaryLink==="function"&&["deduction","net","gross","worked_minutes","slip_minutes","deposit_total"].includes(key))wrap.append(glossaryLink(({worked_minutes:"worked_hours",slip_minutes:"paid_hours",deposit_total:"payment_allocation"})[key]||key,"쉬운 설명")); const meta=node("div",undefined,"field-meta");meta.id="manual-meta-"+key;wrap.append(meta);$("manual-fields").append(wrap);
  }
}
async function loadDocuments() { docs=(await api("/api/documents")).documents.filter(doc=>doc.kind==="upload"); $("document-count").textContent=docs.length; renderDocuments(); }
function renderDocuments() {
  $("documents").replaceChildren();
  if(!docs.length)$("documents").append(node("p","업로드한 PDF가 없습니다. 문서를 추가해 주세요.","library-empty"));
  for(const doc of docs) {
    const button=node("button",undefined,"document-card"+(current?.document_id===doc.id?" selected":""));
    button.dataset.documentId=doc.id; button.setAttribute("aria-pressed",String(current?.document_id===doc.id));
    const meta=node("div",undefined,"doc-meta"); const state=node("span"); state.append(node("i",undefined,"doc-dot"),node("span","내 PDF"));
    meta.append(state,node("span",doc.pages+"p")); button.append(meta,node("strong",doc.title));
    button.addEventListener("click",safeAction(()=>selectDocument(doc.id))); $("documents").append(button);
  }
}
async function selectDocument(docId) {
  if(candidateRequestPending)return;
  if(payrollRoute(activeRoute))await navigateRoute("ai");
  await flushDraft(); clearTimeout(pollTimer); currentJob=null;
  const stored=localStorage.getItem("agent-case-"+docId),existing=docs.find(doc=>doc.id===docId)?.case_id;
  current=null;
  for(const id of new Set([stored,existing].filter(Boolean))){
    try{const restored=await api("/api/cases/"+encodeURIComponent(id));if(restored.document_id===docId){current=restored;break;}if(id===stored)localStorage.removeItem("agent-case-"+docId);}
    catch{if(id===stored)localStorage.removeItem("agent-case-"+docId);}
  }
  if(!current&&existing)throw new Error("기존 문서 처리 결과를 불러오지 못했습니다. 문서 목록을 새로고침한 뒤 다시 선택해 주세요.");
  if(!current) current=await api("/api/cases","POST",{document_id:docId,document_type:"auto"});
  rememberCase();
  notice(""); renderCase();
  await restoreCurrentJob();
}
async function restoreCurrentJob() {
  currentJob=null;
  if(current?.jobs?.length){
    const job=await api("/api/jobs/"+current.jobs.at(-1));
    const obsoleteExtraction=job.stage==="extract"&&current.materials?.sources?.length&&!["queued","running"].includes(job.status);
    if(isComparison()||!obsoleteExtraction&&(job.stage==="ocr"||(job.resolved_document_type||job.document_type||"contract")===(current.document_type||"contract"))&&job.revision>=Math.max(current.document_type_revision||1,current.materials_revision||0))currentJob=job;
  }
  renderJob();if(busy())pollJob();
}
function renderCase() {
  if(!current) return;
  const comparing=isComparison(),type=current.document_type||"contract", isContract=!comparing&&type==="contract", hasContract=isContract||Boolean(current.materials?.selection?.contract_case_id);
  $("document-type").value=type==="payslip_ocr"?"payslip":type;
  $("extract").textContent=type==="auto"?"AI 자동 분류 실행 ↗":"AI 항목 분류 실행 ↗";
  $("model-call-note").hidden=false;
  $("model-call-note").textContent=current.ocr?"기본 읽기 A는 저장된 텍스트만 AI에 전송합니다. 필요한 항목의 PDF 보완 읽기 B는 아래에서 별도로 실행합니다.":"저장된 원문이 없습니다. 원문 읽기 페이지에서 먼저 읽어 주세요.";
  $("contract-inputs").hidden=!hasContract;
  $("contract-period-confirmed").closest("label").hidden=!hasContract;
  if(!isContract) $("manual-section").open=true;
  const doc=docForCase(); renderDocuments();
  if(!comparing){
  $("document-title").textContent=doc?.title||"업로드 PDF"; $("document-kind").textContent="내 PDF";
  const url="/preview.html?document="+current.document_id;
  if($("pdf").dataset.documentId!==current.document_id) { $("pdf").src=url+"#page=1"; $("pdf").dataset.documentId=current.document_id; $("evidence-box").hidden=true; }
  $("pdf").hidden=false; $("pdf-empty").hidden=true; $("open-pdf").hidden=false; $("open-pdf").href=url; $("page-info").textContent=(doc?.pages||"—")+"페이지 · 원문 PDF";
  }
  const extraction=current.extraction; const fields=new Map((extraction?.fields||[]).map(f=>[f.field_id,f]));
  const linkedContract=Boolean(current.materials?.selection?.contract_case_id);
  renderOcr();renderDocumentExtraction(extraction, type);
  for(const key of Object.keys(labels)) {
    $("value-"+key).value=current.draft.values[key]??"";
    const field=linkedContract?current.materials.field_validation?.[key]:fields.get(key),meta=$("meta-"+key);meta.replaceChildren();meta.title="";
    const unit=linkedContract?(key==="contract_rate"?current.draft.contract_unit||current.materials.suggested?.contract_unit:profiles.contract?.fields[key]?.unit):field?.unit;
    meta.append(node("span",unit||(current.draft.values[key]?"원문 값":"미기입 / 미확인")));
    if(linkedContract){
      const refs=current.materials.field_evidence?.[key]||[];
      meta.append(materialEvidenceButtons(refs,current.materials.evidence_catalog));
    }else if(field?.evidence_refs?.length){
      const evidence=extraction.evidence_catalog.find(e=>e.evidence_id===field.evidence_refs[0]);
      if(evidence){const button=node("button","근거 "+evidence.page+"쪽 ↗","evidence-link");button.type="button";button.addEventListener("click",()=>showEvidence(evidence,current.document_id));meta.append(button);}
    }
    renderInputValidation(meta,field,$("value-"+key));
  }
  for(const key of Object.keys(manualLabels)) {
    const input=$("manual-"+key),meta=$("manual-meta-"+key);input.value=current.draft.manual[key]??"";meta.replaceChildren();
    const field=current.materials?.sources?.length?current.materials.field_validation?.[key]:fields.get(key==="deposit"?"deposit_total":key);
    const evidence=field?(current.materials?.sources?.length?materialEvidenceButtons(field.evidence_refs||current.materials.field_evidence?.[key],current.materials.evidence_catalog):evidenceButton(field,extraction)):null;
    renderInputValidation(meta,field,input,evidence);
  }
  $("contract-unit").value=current.draft.contract_unit; $("period-start").value=current.draft.period_start; $("period-end").value=current.draft.period_end;
  for(const key of ["period_start","period_end"]){const meta=$("meta-"+key.replace("_","-"));meta.replaceChildren();const field=current.materials?.sources?.length?current.materials.field_validation?.[key]:fields.get(key);renderInputValidation(meta,field,$(key.replace("_","-")),field?(current.materials?.sources?.length?materialEvidenceButtons(field.evidence_refs,current.materials.evidence_catalog):evidenceButton(field,extraction)):null);}
  $("time-kind").value=current.draft.time_kind;
  $("slip-unit").value=current.draft.slip_unit||"";
  $("contract-period-confirmed").checked=Boolean(current.draft.contract_period_confirmed);
  for(const [id,key] of [["earnings-complete","earnings_complete"],["deductions-complete","deductions_complete"],["payment-confirmed","payment_scope_confirmed"]]) $(id).checked=current.draft[key];
  $("extraction-note").textContent=extraction?"저장 원문으로 정리한 기본 후보 A입니다. 확인 필요·미기입 항목만 골라 PDF로 보완해 읽을 수 있습니다. A·B 후보는 원문을 확인한 뒤 직접 선택하세요.":current.ocr?"원문이 준비되었습니다. ‘AI 처리 실행’을 누르면 문서·항목 정리를 시작합니다.":"먼저 원문 읽기 페이지에서 PDF를 읽어 주세요.";
  if(current.extraction_needs_review)$("extraction-note").textContent="이전 검증 기준의 추출 결과입니다. 수정값은 보존되며, 문서를 다시 추출한 뒤 원문과 확인해 주세요.";
  $("extraction-issues").replaceChildren();
  const issueCount=extractionReviewCount(extraction);
  if(extraction) $("extraction-issues").append(node("p","항목 정리 완료 · "+issueCount+"개 확인 필요. 검사 통과 항목을 포함한 모든 후보는 원문과 사용자 확인이 필요합니다.",issueCount?"issue":"muted"));
  if((extraction?.fields||[]).some(f=>(f.issues||[]).some(issue=>sourceConversions.has(issue)&&issue!=="SOURCE_SCALAR_SPELLING_RESTORED"))) $("extraction-issues").append(node("p","표 머리글의 단위와 원문 표기를 대조해 값을 변환했습니다.","muted"));
  if((extraction?.fields||[]).some(f=>(f.issues||[]).includes("SOURCE_SCALAR_SPELLING_RESTORED"))) $("extraction-issues").append(node("p","시간·지급일의 괄호와 띄어쓰기를 OCR 원문 표기로 맞췄습니다.","muted"));
  const issueLabels={PYTHON_OCR_AI_CLASSIFICATION:"Python OCR → AI 분류가 완료되었습니다. 아래 후보를 확인해 주세요.",OCR_REQUIRES_USER_CONFIRMATION:"OCR로 인식한 값은 원본과 대조해 확인해 주세요.",OCR_LOW_CONFIDENCE:"일부 OCR 문자의 인식 신뢰도가 낮습니다. 해당 페이지의 금액·시간·날짜를 원본에서 확인해 주세요.",OCR_RULE_BASED_EXTRACTION:"이전 Python 규칙 분류 결과입니다. 새 OCR·AI 분류로 다시 추출할 수 있습니다.",OCR_LAYOUT_REQUIRES_MANUAL_REVIEW:"자동으로 분류한 항목이 없습니다. OCR 전체 원문을 보고 직접 입력해 주세요."};
  Object.assign(issueLabels,{PDF_TEXT_AI_CLASSIFICATION:"PDF에 저장된 텍스트로 기본 항목을 정리했습니다. 아래 후보를 확인해 주세요.",SOURCE_REQUIRES_USER_CONFIRMATION:"저장 텍스트와 인용문이 같아도 내용이 정확하다는 뜻은 아닙니다. 원본을 직접 확인해 주세요.",PDF_SUPPLEMENT_REQUIRES_USER_CONFIRMATION:"원본 PDF로 보완해 읽은 후보입니다. A·B값을 직접 확인하고 사용할 값을 선택하세요."});
  for(const issue of extraction?.issues||[]) if(issue!=="VISION_REQUIRES_USER_CONFIRMATION") $("extraction-issues").append(node("p",issueLabels[issue]||issue,"issue"));
  renderExtractionChanges(); renderDraftHistory(); renderMaterials(); renderMaterialReview(); renderComparisonContext(); if(typeof renderWageForm==="function")renderWageForm(); renderResults(); renderJob();
  if(comparisonSources===null&&!comparisonSourcesRequest) loadComparisonSources().catch(error=>notice("자료 목록을 불러오지 못했습니다. "+error.message,true));
}
function renderOcrPages(target, pages) {
  target.replaceChildren();
  if(!pages?.length){const empty=node("div",undefined,"stage-empty");empty.append(node("h3","저장된 원문이 아직 없습니다"),node("p","원문 읽기 페이지에서 PDF를 선택하고 읽기를 실행해 주세요."));target.append(empty);return;}
  pages.forEach((text,index)=>{const page=node("section",undefined,"ocr-page-text");page.append(node("h3",(index+1)+"페이지"),node("pre",text||"인식된 문자가 없는 페이지입니다.","ocr-text"));target.append(page);});
}
function renderOcr() {
  const pages=current?.ocr?.pages||[];
  renderOcrPages($("ocr-pages"),pages);renderOcrPages($("ai-ocr-pages"),pages);
  $("ocr-state").textContent=pages.length?"저장 완료 · "+pages.length+"페이지":"추출 전";
  $("ocr").textContent=current?.ocr?"원문 다시 읽기":"원문 읽기 실행";
  $("ai-state").textContent=current?.extraction?"항목 정리 완료 · "+extractionReviewCount(current.extraction)+"개 확인 필요":current?.ocr?"원문 준비 완료":"원문 읽기 필요";
}
function evidenceButton(field, extraction) {
  const evidence=(extraction?.evidence_catalog||[]).find(e=>(field.evidence_refs||[]).includes(e.evidence_id));
  if(!evidence) return null;
  const button=node("button","근거 "+evidence.page+"쪽 ↗","evidence-link"); button.type="button";
  button.addEventListener("click",()=>showEvidence(evidence,current.document_id)); return button;
}
function extractionInput(change) {
  return change.group==="values"?$("value-"+change.field_id):change.group==="manual"?$("manual-"+change.field_id):$({contract_unit:"contract-unit",slip_unit:"slip-unit",period_start:"period-start",period_end:"period-end"}[change.field_id]);
}
function renderExtractionChanges() {
  const target=$("extraction-changes"),changes=current?.extraction_changes||[];
  target.replaceChildren();target.hidden=!changes.length;
  if(!changes.length)return;
  target.append(node("strong","재추출 전 입력 "+changes.length+"개를 유지했습니다."),node("p","직접 수정하거나 확인한 값을 새 추출값으로 바꾸지 않았습니다. 원문을 확인한 뒤 사용할 값을 선택하고 입력값을 다시 확인하세요.","muted"));
  for(const change of changes){
    const input=extractionInput(change);if(!input)continue;
    const label=labels[change.field_id]||manualLabels[change.field_id]||{contract_unit:"계약 임금 단위",slip_unit:"명세서 단위",period_start:"급여 시작일",period_end:"급여 종료일"}[change.field_id]||change.field_id;
    const row=node("div",undefined,"candidate-change"),values=node("div",undefined,"candidate-values");row.append(node("h4",label));
    for(const [title,value] of [["유지한 입력",change.preserved],["새 추출 후보",change.candidate]]){const cell=node("div");cell.append(node("small",title),node("strong",value==null||value===""?"미기입":String(value)));values.append(cell);}row.append(values);
    const actions=node("div",undefined,"candidate-actions");
    const validation=current.materials?.sources?.length?current.materials.field_validation?.[change.field_id]:current.extraction?.fields?.find(field=>field.field_id===change.field_id);
    for(const [title,key] of [["유지한 입력 사용","preserved"],["새 추출값 사용","candidate"]]){if(key==="candidate"&&validation&&fieldValidationStatus(validation)!=="valid")continue;const button=node("button",title);button.type="button";button.disabled=Boolean(current?.materials_invalid||busy());button.setAttribute("aria-pressed",String(String(change[key]??"")===input.value));button.addEventListener("click",()=>{const pair={contract_rate:"contract_unit",contract_unit:"contract_rate",slip_rate:"slip_unit",slip_unit:"slip_rate"}[change.field_id];for(const item of changes.filter(item=>item===change||(pair&&item.field_id===pair))){const field=extractionInput(item);if(field)field.value=item[key]??"";}changed();});actions.append(button);}row.append(actions);if(validation&&fieldValidationStatus(validation)!=="valid")row.append(node("p","새 후보는 확인이 필요합니다. 원문을 보고 아래 입력란에서 직접 수정하거나 빈값으로 보류하세요.","muted"));target.append(row);
  }
}
function renderDraftHistory() {
  const target=$("draft-history"),history=current?.draft_history||[];
  target.replaceChildren();target.hidden=!history.length;
  if(!history.length)return;
  target.append(node("summary","이전 입력 기록 · "+history.length+"건"),node("p","재추출하거나 자료를 다시 불러오기 전의 입력입니다. 원문과 같은 항목인지 확인한 뒤 필요한 값을 직접 반영하세요.","muted"));
  for(const entry of [...history].reverse()){
    const details=node("details",undefined,"draft-history-entry"),draft=entry.draft||{},rows=[];
    const date=new Date(entry.saved_at),stamp=Number.isNaN(date.getTime())?"이전 입력":date.toLocaleString("ko-KR",{timeZone:"Asia/Seoul"});details.append(node("summary",stamp));
    for(const [key,label] of [["period_start","급여 시작일"],["period_end","급여 종료일"]])if(draft[key])rows.push([label,draft[key]]);
    for(const [group,definitions] of [["values",labels],["manual",manualLabels]])for(const [key,value] of Object.entries(draft[group]||{}))if(value!=null&&value!=="")rows.push([definitions[key]||"확인한 값",String(value)]);
    const review=draft.material_review||{};
    for(const [group,label] of [["earnings","지급"],["deductions","공제"]])for(const [index,[id,amount]] of Object.entries(review[group]||{}).entries()){
      const source=entry.materials?.[group]?.find(row=>row.item_id===id);rows.push([label+" · "+(source?.label||"항목 "+(index+1)),(amount??"미확인")+" · "+(statementRoleLabels[review.row_roles?.[id]||source?.row_role||"detail"])]);
    }
    for(const row of review.manual_rows||[])rows.push(["수기 보완 · "+(row.label||"항목명 미입력"),(row.amount??"미확인")+" · 원문 "+row.page+"쪽 · "+statementRoleLabels[row.row_role||"detail"]]);
    for(const [id,row] of Object.entries(review.transactions||{}))if(row.selected){const source=entry.materials?.transactions?.find(item=>item.transaction_id===id);rows.push(["입금 배분 · "+(source?.date||"날짜 미확인"),row.amount??"미확인"]);}
    const list=node("dl");for(const [name,value] of rows)list.append(node("dt",name),node("dd",value));details.append(rows.length?list:node("p","기록된 숫자·기간 입력이 없습니다.","muted"));target.append(details);
  }
}
function candidateBase() {return current?.extraction_a||current?.extraction;}
function candidateDefinition(fieldId) {const profile=profiles[current?.document_type]||{},match=/^([a-z]+)_[1-9]\d{0,3}_([a-z_]+)$/.exec(fieldId);return profile.fields?.[fieldId]||(match?profile.rows?.[match[1]]?.fields?.[match[2]]:null)||{};}
function originalCandidate(fieldId) {return candidateBase()?.fields?.find(field=>field.field_id===fieldId);}
function candidateSource(field,payload) {const evidence=(payload?.evidence_catalog||[]).find(item=>(field?.evidence_refs||[]).includes(item.evidence_id));return {quote:field?.source_quote||evidence?.quote||"",page:field?.source_page||evidence?.page||null};}
function supplementEligible(field) {const original=originalCandidate(field.field_id);return original&&["needs_review","missing"].includes(fieldValidationStatus(original))&&["needs_review","missing"].includes(fieldValidationStatus(field));}
function supplementCheckbox(field) {
  const check=node("input");check.type="checkbox";check.checked=supplementSelection.has(field.field_id);check.disabled=busy()||candidateRequestPending||Boolean(current?.extraction_needs_review);check.dataset.supplementField=field.field_id;
  check.setAttribute("aria-label",fieldReviewLabel(field)+" 보완 읽기");
  check.addEventListener("change",()=>{if(check.checked)supplementSelection.add(field.field_id);else supplementSelection.delete(field.field_id);updateSupplementControls();});return check;
}
function renderSupplementFields() {
  const target=$("supplement-fields"),empty=$("supplement-empty");target.replaceChildren();
  const fields=current?.extraction&&!isComparison()&&!current.extraction_needs_review?current.extraction.fields.filter(supplementEligible):[];
  const eligible=new Set(fields.map(field=>field.field_id));for(const id of supplementSelection)if(!eligible.has(id))supplementSelection.delete(id);
  empty.hidden=fields.length>0;empty.textContent=current?.extraction_needs_review?"이전 추출 기준의 결과입니다. 기본 항목 정리를 다시 완료한 뒤 보완할 항목을 선택해 주세요.":current?.extraction?"보완할 미확인 항목이 없습니다. 이미 확인한 값은 아래에서 원문을 보고 직접 수정할 수 있습니다.":"기본 항목 정리를 완료하면 보완할 항목이 표시됩니다.";
  for(const field of fields){
    const label=node("label",undefined,"supplement-target"),text=node("span",undefined,"supplement-target-text"),source=candidateSource(originalCandidate(field.field_id),candidateBase());
    text.append(node("strong",fieldReviewLabel(field)),node("small",validationLabels[fieldValidationStatus(field)]+(source.page?" · "+source.page+"쪽":"")));
    const raw=field.raw_value==null||field.raw_value===""?"읽은 값 없음":String(field.raw_value);text.append(node("small","기본 읽기: "+(raw.length>160?raw.slice(0,160)+"…":raw)));
    label.append(supplementCheckbox(field),text);target.append(label);
  }
}
function candidateUsable(field,payload,isSupplement=false) {const source=candidateSource(field,payload),pages=docForCase()?.pages||payload?.page_count||current?.ocr?.pages?.length||0;return Boolean(field&&fieldValidationStatus(field)==="valid"&&field.normalized_value!=null&&(!isSupplement||(source.quote.trim()&&Number.isInteger(source.page)&&source.page>0&&source.page<=pages)));}
function candidateOriginButton(field,payload,label="원문 보기 ↗") {const source=candidateSource(field,payload),pages=docForCase()?.pages||payload?.page_count||current?.ocr?.pages?.length||0,validPage=Number.isInteger(source.page)&&source.page>0&&source.page<=pages;const button=node("button",label,"evidence-link");button.type="button";button.addEventListener("click",()=>openSourceDocument(current.document_id,docForCase()?.title,validPage?source.page:1));return button;}
function candidateValue(field) {return field?.normalized_value??field?.suggested_value??field?.raw_value??null;}
function renderCandidateChoice(field,payload,kind,candidateId,stale=false) {
  const card=node("div",undefined,"candidate-choice"),source=candidateSource(field,payload);card.append(node("strong",kind==="A"?"기본 읽기 A · 저장 원문":"보완 읽기 B · 원본 PDF"));appendValidationDetails(card,field,false,kind==="B");
  if(source.quote)card.append(node("small",(source.page?"AI가 인용한 "+source.page+"쪽 문장":"AI가 인용한 문장")+" · 원본 확인 필요"),node("blockquote",source.quote));else card.append(node("p","인용문 없음 · 원본에서 직접 확인해 주세요.","muted"));
  card.append(candidateOriginButton(field,payload));
  const selected=current.candidate_selections?.[field.field_id],selectedId=typeof selected==="string"?selected:selected?.candidate_id;
  if(selectedId===candidateId)card.append(node("span","이번 항목에서 선택한 값","validation-status"));
  const use=node("button","원문 확인 후 "+kind+"값 사용");use.type="button";use.dataset.candidateAction="true";use.dataset.candidateAllowed=String(!stale&&candidateUsable(field,payload,kind==="B"));use.disabled=busy()||candidateRequestPending||use.dataset.candidateAllowed!=="true";use.addEventListener("click",safeAction(()=>saveCandidateSelection(field.field_id,candidateId)));card.append(use);
  if(stale)card.append(node("small","이전 기본 읽기에 대한 보완 결과입니다. 현재 항목에 사용할 수 없습니다.","validation-reason"));else if(!candidateUsable(field,payload,kind==="B"))card.append(node("small","이 후보는 바로 사용할 수 없습니다. 원문을 확인해 직접 입력하거나 보류하세요.","muted"));
  return card;
}
function appendCandidateEditor(cell,field) {
  const original=originalCandidate(field.field_id);if(!original||isComparison())return;
  if(supplementEligible(field)){const label=node("label",undefined,"supplement-field-select");label.append(supplementCheckbox(field),node("span","이 항목 PDF로 보완 읽기"));cell.prepend?cell.prepend(label):cell.append(label);}
  const candidates=(current.supplements||[]).filter(item=>item.payload?.fields?.some(value=>value.field_id===field.field_id)),latest=candidates.at(-1);
  if(latest){const alternative=latest.payload.fields.find(value=>value.field_id===field.field_id),left=candidateValue(original),right=candidateValue(alternative),agreement=left==null||right==null?"한쪽 값 없음":String(left)===String(right)&&original.unit===alternative.unit?"A·B 값 일치":"A·B 값 다름";const comparison=node("section",undefined,"candidate-comparison");comparison.append(node("strong",agreement+" · 원문 확인 필요"),node("p","두 읽기의 값이 같아도 사용자 확인 전 후보입니다.","muted"));const pair=node("div",undefined,"candidate-choice-grid");pair.append(renderCandidateChoice(original,candidateBase(),"A","A"),renderCandidateChoice(alternative,latest.payload,"B",latest.id+":"+field.field_id,latest.base_extraction_revision!==(current.extraction_a_revision??current.extraction_revision)));comparison.append(pair);cell.append(comparison);}
  const definition=candidateDefinition(field.field_id),rate=definition.unit==="rate",details=node("details",undefined,"candidate-manual"),summary=node("summary","원문 보고 직접 수정 · 보류"),label=node("label","직접 확인한 값","field"),input=node("input"),unitLabel=node("label","단위","field"),unit=node(rate?"select":"input");input.setAttribute("aria-label",fieldReviewLabel(original)+" 직접 확인한 값");input.placeholder=original.unit==="date"?"YYYY-MM-DD":original.unit==="minute"?"분 단위 정수":"원문에서 확인한 값";if(rate){for(const [value,text] of [["","단위 선택"],["KRW/hour","원 / 시간"],["KRW/month","원 / 월"],["KRW/year","원 / 년"],["KRW/day","원 / 일"]]){const option=node("option",text);option.value=value;unit.append(option);}}else unit.readOnly=true;unit.value=original.unit||definition.unit||"";unit.setAttribute("aria-label",fieldReviewLabel(original)+" 직접 입력 단위");label.append(input);unitLabel.append(unit);details.append(summary,candidateOriginButton(original,candidateBase()),label,unitLabel,node("p","금액은 쉼표 없는 숫자, 시간은 분 단위로 입력하세요. 날짜는 YYYY-MM-DD 형식입니다.","muted"));
  const actions=node("div",undefined,"field-review-actions"),save=node("button","원문 확인 후 직접 입력값 사용"),defer=node("button","이번 선택 보류");save.type=defer.type="button";save.dataset.candidateAction=defer.dataset.candidateAction="true";save.dataset.candidateAllowed="false";defer.dataset.candidateAllowed="true";save.disabled=true;input.addEventListener("input",()=>{save.dataset.candidateAllowed=String(Boolean(input.value.trim()));save.disabled=busy()||candidateRequestPending||!input.value.trim();});save.addEventListener("click",safeAction(()=>saveCandidateSelection(field.field_id,"manual",input.value.trim(),unit.value.trim())));defer.addEventListener("click",()=>{input.value="";save.disabled=true;supplementSelection.delete(field.field_id);renderDocumentExtraction(current.extraction,current.document_type);notice("새 값을 선택하지 않고 보류했습니다. 기존에 확인한 값은 유지됩니다.");});actions.append(save,defer);details.append(actions);cell.append(details);
}
function extractedValue(field, definition, extraction) {
  const cell=node("div",undefined,"extracted-value");appendValidationDetails(cell,field);if(field.unit)cell.append(node("small","단위: "+field.unit));const evidence=evidenceButton(field,extraction);if(evidence)cell.append(evidence);appendCandidateEditor(cell,field);return cell;
}
function updateSupplementControls() {
  const available=Boolean(current?.extraction&&!isComparison()),running=busy()||candidateRequestPending;$("supplement-panel").hidden=!available;$("supplement-count").textContent=supplementSelection.size+"개 항목 선택 · 한 번에 최대 50개";$("supplement").disabled=!available||running||!supplementSelection.size||supplementSelection.size>50||Boolean(current?.extraction_needs_review);
  document.querySelectorAll("[data-supplement-field]").forEach(input=>{input.checked=supplementSelection.has(input.dataset.supplementField);input.disabled=running||Boolean(current?.extraction_needs_review);});
  document.querySelectorAll("[data-candidate-action]").forEach(button=>{button.disabled=running||button.dataset.candidateAllowed!=="true";});
  document.querySelectorAll(".candidate-manual input").forEach(input=>{input.disabled=running;});
}
async function startSupplement() {
  if(!current||!supplementSelection.size||busy()||candidateRequestPending)return;const id=current.id,revision=current.extraction_revision,field_ids=[...supplementSelection];candidateRequestPending=true;updateSupplementControls();
  try{await flushDraft();if(current?.id!==id)return;const job=await api("/api/cases/"+id+"/jobs","POST",{stage:"supplement",field_ids,extraction_revision:revision});if(current?.id===id){currentJob=job;notice("선택한 항목을 원본 PDF로 보완해 읽고 있습니다. 완료 후 A·B값을 직접 확인하세요.");renderJob();pollJob();}}finally{candidateRequestPending=false;updateSupplementControls();}
}
async function saveCandidateSelection(fieldId,candidateId,value,unit) {
  if(!current||busy()||candidateRequestPending)return;const id=current.id,extraction_revision=current.extraction_revision;candidateRequestPending=true;updateSupplementControls();
  try{const body={field_id:fieldId,candidate_id:candidateId,source_confirmed:true,extraction_revision};if(candidateId==="manual")Object.assign(body,{value,unit});const saved=await api("/api/cases/"+id+"/candidate-selection","POST",body);if(current?.id===id){current=saved;syncActiveRecord();renderCase();await loadComparisonSources();notice("선택한 후보를 저장했습니다. 기존에 직접 입력한 값은 유지됩니다. 문서 대조에서 적용할 값을 다시 확인해 주세요.");}}finally{candidateRequestPending=false;updateSupplementControls();}
}
function renderDocumentExtraction(extraction, type) {
  const key=current?.id+":"+current?.extraction_revision;if(key!==supplementSelectionKey){supplementSelectionKey=key;supplementSelection=new Set();}renderSupplementFields();updateSupplementControls();
  const container=$("document-extraction");container.replaceChildren();container.hidden=false;
  if(!extraction){container.append(node("p","AI 처리 후 문서 분류와 항목 후보가 여기에 표시됩니다.","muted"));return;}
  const classification=node("div",undefined,"classification-summary");classification.append(node("small","AI 문서 분류"),node("strong",profiles[type==="payslip_ocr"?"payslip":type]?.label||type),node("span","기본 읽기 A는 저장된 원문으로 항목을 정리합니다. 보완 읽기 B와 값이 같아도 사용자 확인이 필요합니다."));container.append(classification);
  const profile=profiles[type]||{fields:{},rows:{}}, summary=node("div",undefined,"extracted-summary");
  const groups=new Map(),extras=[];
  for(const field of extraction.fields) {
    const definition=profile.fields[field.field_id];
    if(definition){const item=node("div");item.append(node("strong",definition.label),extractedValue(field,definition,extraction));summary.append(item);continue;}
    const match=/^([a-z]+)_([1-9]\d{0,3})_([a-z_]+)$/.exec(field.field_id);
    if(match&&profile.rows[match[1]]?.fields[match[3]]){
      if(!groups.has(match[1]))groups.set(match[1],new Map());
      const rows=groups.get(match[1]);if(!rows.has(Number(match[2])))rows.set(Number(match[2]),new Map());rows.get(Number(match[2])).set(match[3],field);
    }else extras.push(field);
  }
  container.append(node("h3","문서 요약"),summary);
  for(const [prefix,definition] of Object.entries(profile.rows)) {
    const rows=groups.get(prefix)||new Map();container.append(node("h3",definition.label+" · "+rows.size+"행"));
    if(!rows.size){container.append(node("p","추출된 상세 행이 없습니다. 원문을 확인해 주세요.","muted"));continue;}
    const scroll=node("div",undefined,"table-scroll extracted-rows"),table=node("table"),head=node("tr");
    head.append(node("th","행"));for(const column of Object.values(definition.fields))head.append(node("th",column.label));table.append(head);
    for(const [index,fields] of [...rows].sort((a,b)=>a[0]-b[0])){
      const row=node("tr");row.append(node("td",String(index)));
      for(const [key,column] of Object.entries(definition.fields)){
        const td=node("td");td.append(fields.has(key)?extractedValue(fields.get(key),column,extraction):node("span","미기입"));row.append(td);
      }table.append(row);
    }scroll.append(table);container.append(scroll);
  }
  if(extras.length){const details=node("details");details.append(node("summary","추가 추출 항목 "+extras.length+"개"));for(const field of extras){details.append(node("strong",field.field_id),extractedValue(field,{},extraction));}container.append(details);}
  container.append(node("p","모든 항목은 사용자 확인 전 후보입니다. 검사 통과는 OCR 텍스트와 형식 검사 결과이며 원본의 정확성을 보장하지 않습니다. 문서 대조 페이지에서 원문을 확인하고 입력값을 확정하세요.","muted"));
}
async function loadComparisonSources() {
  if(comparisonSourcesRequest)return comparisonSourcesRequest;
  comparisonSourcesRequest=(async()=>{comparisonSources=(await api("/api/comparison-sources")).sources;renderMaterials();if(comparisonMode)renderComparisonPicker();updateControls();})();
  try{await comparisonSourcesRequest;}finally{comparisonSourcesRequest=null;}
}
function sourceTitle(source) {
  const period=source.period_start||source.period_end?" · "+(source.period_start||"미확인")+" ~ "+(source.period_end||"미확인"):"";
  return (source.title||source.case_id)+" · "+(profiles[source.document_type]?.label||source.document_type)+period+(!source.has_extraction?" · 추출 필요":"");
}
function renderMaterials() {
  if(comparisonMode||isComparison())return renderComparisonPicker();
  if(!current){$("materials-selectors").replaceChildren(node("p","먼저 PDF를 업로드하고 OCR과 AI 처리를 완료해 주세요.","muted"));return;}
  const target=$("materials-selectors"),selection=current.materials?.selection||{};
  target.replaceChildren();materialSelectors={};
  if(comparisonSources===null){target.append(node("p","추출한 자료 목록을 불러오는 중입니다.","muted"));return;}
  const definitions=[{key:"contract_case_id",label:"계약서",types:["contract"]},{key:"attendance_case_ids",label:"근무기록",types:["attendance"],multiple:true},{key:"payslip_case_id",label:"급여명세서",types:["payslip","payslip_ocr"]},{key:"deposit_case_ids",label:"입금기록",types:["deposit"],multiple:true}];
  for(const definition of definitions){
    const sources=comparisonSources.filter(source=>definition.types.includes(source.document_type));
    if(definition.multiple){
      const group=node("fieldset",undefined,"material-source-group"),legend=node("legend",definition.label+" · 여러 건 선택");group.append(legend);materialSelectors[definition.key]=[];
      if(!sources.length)group.append(node("p","등록된 자료가 없습니다.","muted"));
      for(const source of sources){const label=node("label",undefined,"checkbox"),input=node("input");input.type="checkbox";input.value=source.case_id;input.checked=(selection[definition.key]||[]).includes(source.case_id);input.disabled=!source.has_extraction||busy()||loadingMaterials;input.dataset.materialSource="1";input.addEventListener("change",materialSelectionChanged);label.append(input,node("span",sourceTitle(source)));group.append(label);materialSelectors[definition.key].push(input);}
      target.append(group);
    }else{
      const label=node("label",definition.label,"field"),select=node("select"),empty=node("option","연결 안 함");empty.value="";select.append(empty);select.setAttribute("aria-label",definition.label+" 자료 선택");
      for(const source of sources){const option=node("option",sourceTitle(source));option.value=source.case_id;option.disabled=!source.has_extraction;select.append(option);}
      select.value=selection[definition.key]||"";select.disabled=busy()||loadingMaterials;select.dataset.materialSource="1";select.addEventListener("change",materialSelectionChanged);materialSelectors[definition.key]=select;label.append(select);target.append(label);
    }
  }
  $("materials-state").textContent=current.materials_invalid?"연결한 원본이 변경되었습니다. 자료를 다시 선택하고 ‘선택 자료 불러오기’를 눌러 주세요.":current.materials?.sources?.length?"자료 "+current.materials.sources.length+"건 연결됨 · 자료·값 확인 → 입력값 확인 → 자료 대조 순서로 진행합니다.":"연결할 자료를 선택하세요. 모두 ‘연결 안 함’으로 불러오면 연결이 해제됩니다.";
  $("materials-warnings").replaceChildren();
  for(const warning of current.materials?.warnings||[])$("materials-warnings").append(node("p",warning,"issue"));
  if(current.materials?.sources?.length){const list=node("div",undefined,"linked-source-list");for(const source of current.materials.sources){const button=node("button",(profiles[source.document_type]?.label||"자료")+" · "+(source.title||source.case_id)+" 원문 ↗","evidence-link");button.type="button";button.addEventListener("click",()=>openSourceDocument(source.document_id,source.title));list.append(button);}target.append(list);}
}
function selectedMaterials() {
  if(comparisonMode||isComparison())return structuredClone(comparisonSelection);
  return {contract_case_id:materialSelectors.contract_case_id?.value||null,attendance_case_ids:(materialSelectors.attendance_case_ids||[]).filter(input=>input.checked).map(input=>input.value),payslip_case_id:materialSelectors.payslip_case_id?.value||null,deposit_case_ids:(materialSelectors.deposit_case_ids||[]).filter(input=>input.checked).map(input=>input.value)};
}
function materialsSelectionPending() {
  if(comparisonMode||isComparison())return Boolean(isComparison()&&!sameComparisonSelection(comparisonSelection,current.materials?.selection));
  if(!current||!materialSelectors.contract_case_id)return false;
  const selected=selectedMaterials(),saved=current.materials?.selection||{};
  return Object.keys(selected).some(key=>Array.isArray(selected[key])?JSON.stringify([...selected[key]].sort())!==JSON.stringify([...(saved[key]||[])].sort()):(selected[key]||null)!==(saved[key]||null));
}
function materialSelectionChanged() {
  renderResults();
  $("materials-state").textContent=materialsSelectionPending()?"파일 선택이 바뀌었습니다. ‘선택 자료 불러오기’를 눌러 적용한 뒤 값을 확인해 주세요.":"연결한 파일과 같은 선택입니다.";
  $("materials-state").className="muted"+(materialsSelectionPending()?" selection-pending":"");
}
async function applyMaterials() {
  if(comparisonMode||isComparison())return applyComparisonSelection();
  if(!current||comparisonSources===null)return;
  const selection=selectedMaterials(),id=current.id;loadingMaterials=true;updateControls();
  try{if(current.materials_invalid){clearTimeout(draftTimer);pendingDraft=false;}else await flushDraft();const saved=await api("/api/cases/"+id+"/materials","PUT",selection);if(current?.id!==id)return;current=saved;currentJob=null;clearTimeout(pollTimer);renderCase();await loadComparisonSources();showConfirmation();notice(current.materials?.sources?.length?"선택한 자료를 불러왔습니다. 아래 값을 확인하고 ‘입력값 확인’을 누르면 자료 대조를 실행할 수 있습니다.":"자료 연결을 해제했습니다.");}
  finally{loadingMaterials=false;updateControls();}
}
function materialEvidenceButtons(refs, catalog) {
  const group=node("span",undefined,"material-evidence");
  const pages=new Map();for(const id of new Set(refs||[])){const evidence=(catalog||[]).find(item=>item.evidence_id===id);if(!evidence)continue;const key=evidence.source_id+"|"+evidence.page+"|"+evidence.verification_method,items=pages.get(key)||[];items.push(evidence);pages.set(key,items);}
  for(const items of pages.values()){const evidence={...items[0],quote:[...new Set(items.map(item=>item.quote).filter(Boolean))].join("\n")};const button=node("button",evidence.page?"근거 "+evidence.page+"쪽 ↗":"확인 근거 ↗","evidence-link");button.type="button";button.addEventListener("click",()=>showEvidence(evidence));group.append(button);}
  return group;
}
function statementRoleSelect(id, value) {
  const label=node("label","합계 반영 방식","field"),select=node("select");
  select.id=id;label.htmlFor=id;
  for(const [key,text] of Object.entries(statementRoleLabels)){const option=node("option",text);option.value=key;select.append(option);}
  select.value=Object.hasOwn(statementRoleLabels,value)?value:"detail";select.addEventListener("change",changed);label.append(select);
  return {label,select};
}
function statementCents(value) {
  if(!/^-?\d{1,13}(?:\.\d{1,2})?$/.test(value||""))return null;
  const negative=value.startsWith("-"),[whole,fraction=""]=value.replace(/^-/ ,"").split(".");
  const cents=BigInt(whole)*100n+BigInt(fraction.padEnd(2,"0"));return negative?-cents:cents;
}
function statementAmount(value) {
  const negative=value<0n,absolute=negative?-value:value,part=String(absolute%100n).padStart(2,"0").replace(/0+$/,"");
  return (negative?"−":"")+(absolute/100n).toLocaleString("ko-KR")+(part?"."+part:"")+"원";
}
function updateStatementTotals() {
  for(const group of ["earnings","deductions"]){
    const target=$("statement-summary-"+group);if(!target||!current?.materials)continue;
    const rows=[...(current.materials[group]||[]).map(item=>({amount:materialInputs[group].get(item.item_id)?.value||"",role:materialInputs.row_roles.get(item.item_id)?.value||item.row_role||"detail"})),...[...materialInputs.manual_rows.values()].filter(row=>row.group===group).map(row=>({amount:row.amount.value,role:row.row_role.value}))];
    let sum=0n,missing=0,detail=0,totals=0,excluded=0;
    for(const row of rows){if(row.role==="excluded"){excluded++;continue;}if(row.role!=="detail"){totals++;continue;}detail++;const amount=statementCents(row.amount.trim());if(amount===null)missing++;else sum+=amount;}
    target.replaceChildren(node("span",missing?"세부항목 합계 · 미확인 금액 있음":"세부항목 합계 · 확인용"),node("strong",detail?statementAmount(sum):"세부항목 없음"),node("small","세부 "+detail+"개 · 소계·총계 "+totals+"개 제외 · 대조 제외 "+excluded+"개"+(missing?" · 미확인 "+missing+"개":"")));
    target.className="statement-summary"+(missing?" incomplete":"");
  }
  for(const select of materialInputs.row_roles.values()){const row=select.closest(".statement-row");if(row)row.dataset.rowRole=select.value;}
  for(const inputs of materialInputs.manual_rows.values()){const row=inputs.row_role.closest(".statement-row");if(row)row.dataset.rowRole=inputs.row_role.value;}
}
function addManualStatementRow(group) {
  current.draft=readForm();
  current.draft.material_review.manual_rows.push({row_id:"row-"+Date.now().toString(36)+"-"+(++manualRowSequence),group,label:"",amount:null,row_role:"detail",page:1});
  renderMaterialReview();changed();
  const row=[...materialInputs.manual_rows.values()].at(-1);if(row&&typeof row.label.focus==="function")row.label.focus();
}
function renderMaterialReview() {
  const target=$("material-review"),materials=current.materials,review=current.draft.material_review||{};
  target.replaceChildren();materialInputs={earnings:new Map(),deductions:new Map(),transactions:new Map(),row_roles:new Map(),manual_rows:new Map()};distinctTransactionsInput=null;
  target.hidden=!materials?.sources?.length;
  const hasPayslip=Boolean(materials?.selection?.payslip_case_id),hasDeposits=Boolean(materials?.selection?.deposit_case_ids?.length);
  for(const key of ["earning_item","deduction_item"])$("manual-"+key).closest("label").hidden=hasPayslip;
  $("manual-deposit").closest("label").hidden=hasDeposits;
  if(target.hidden)return;
  target.append(node("h3","연결한 자료의 상세 항목 확인"),node("p","금액과 항목 분류를 원문과 대조하세요. 소계·총계는 다시 더하지 않으며, 잘못 추출된 행은 대조에서 제외할 수 있습니다. 비어 있는 금액은 미확인으로 남습니다.","muted"));
  for(const [key,label] of [["earnings","지급 항목"],["deductions","공제 항목"]]){
    if(!hasPayslip)continue;
    const heading=node("div",undefined,"statement-group-heading"),add=node("button","＋ 누락 항목 보완");add.type="button";add.setAttribute("aria-label",label+" 누락 항목 보완");add.addEventListener("click",()=>addManualStatementRow(key));heading.append(node("h4",label),add);target.append(heading);
    const summary=node("div",undefined,"statement-summary");summary.id="statement-summary-"+key;summary.setAttribute("aria-live","polite");target.append(summary);
    const items=materials[key]||[];
    if(!items.length)target.append(node("p","추출된 "+label+"이 없습니다. 원문에 있는 행을 ‘누락 항목 보완’으로 추가하세요. 항목이 없음을 확인한 경우도 확인 범위를 직접 선택하세요.","issue"));
    for(const [index,item] of items.entries()){
      const row=node("div",undefined,"statement-row"),title=node("div",undefined,"statement-row-heading");title.append(node("strong",item.label||label+" "+(index+1)),node("span","추출 후보","statement-origin"));row.append(title);
      const fields=node("div",undefined,"statement-row-fields"),labelNode=node("label","금액 (원)","field"),input=node("input");input.id="material-"+key+"-"+index;input.inputMode="decimal";input.placeholder="미확인 · 원문 확인 후 입력";input.value=Object.hasOwn(review[key]||{},item.item_id)?review[key][item.item_id]??"":item.amount??"";labelNode.htmlFor=input.id;labelNode.append(input);
      const role=statementRoleSelect("statement-role-"+key+"-"+index,review.row_roles?.[item.item_id]||item.row_role||(item.is_total?"total":"detail"));fields.append(labelNode,role.label);row.append(fields,materialEvidenceButtons(item.evidence_refs,materials.evidence_catalog));appendMaterialWarnings(row,item,input);if(hasNonAmountWarnings(item)){row.append(node("p","항목명 등 금액 이외의 내용이 잘못되었다면 이 행을 대조에서 제외하고 ‘누락 항목 보완’으로 원문 값을 입력하세요. 금액만 입력해도 이 행의 오류가 해소되지는 않습니다.","muted"));const exclude=node("button","이 행 제외");exclude.type="button";exclude.addEventListener("click",()=>{role.select.value="excluded";changed();notice("이 행을 대조에서 제외했습니다. 필요한 항목은 ‘누락 항목 보완’으로 원문 값을 직접 입력하세요.");});row.append(exclude);}target.append(row);materialInputs[key].set(item.item_id,input);materialInputs.row_roles.set(item.item_id,role.select);
    }
    for(const item of (review.manual_rows||[]).filter(item=>item.group===key)){
      const row=node("div",undefined,"statement-row manual-statement-row"),title=node("div",undefined,"statement-row-heading"),remove=node("button","삭제");remove.type="button";remove.setAttribute("aria-label","수기 보완 항목 삭제");remove.addEventListener("click",()=>{current.draft=readForm();current.draft.material_review.manual_rows=current.draft.material_review.manual_rows.filter(value=>value.row_id!==item.row_id);renderMaterialReview();changed();});title.append(node("span","수기 보완","statement-origin"),remove);row.append(title);
      const labelNode=node("label","원문 항목명","field"),name=node("input");name.id="manual-row-label-"+item.row_id;name.value=item.label;name.maxLength=120;name.placeholder="예: 식대";labelNode.htmlFor=name.id;labelNode.append(name);row.append(labelNode);
      const fields=node("div",undefined,"statement-row-fields"),amountLabel=node("label","금액 (원)","field"),amount=node("input");amount.id="manual-row-amount-"+item.row_id;amount.inputMode="decimal";amount.value=item.amount??"";amount.placeholder="미확인";amountLabel.htmlFor=amount.id;amountLabel.append(amount);
      const role=statementRoleSelect("manual-row-role-"+item.row_id,item.row_role);fields.append(amountLabel,role.label);row.append(fields);
      const pageLabel=node("label","원문 페이지","field manual-row-page"),page=node("input"),source=materials.sources.find(source=>source.case_id===materials.selection.payslip_case_id);page.id="manual-row-page-"+item.row_id;page.type="number";page.min="1";page.max=String(source?.page_count||docs.find(doc=>doc.id===source?.document_id)?.pages||1);page.value=String(item.page||1);pageLabel.htmlFor=page.id;pageLabel.append(page);const open=node("button","원문 확인 ↗","evidence-link");open.type="button";open.addEventListener("click",()=>{const value=Number(page.value);if(Number.isInteger(value)&&value>=1&&value<=Number(page.max))openSourceDocument(source?.document_id,source?.title,value);});row.append(pageLabel,open,node("small","직접 보완한 값으로 기록됩니다. 원문에 있는 항목만 추가하세요.","muted"));target.append(row);materialInputs.manual_rows.set(item.row_id,{group:key,label:name,amount,row_role:role.select,page});
    }
  }
  if(hasDeposits){
    target.append(node("h4","이 급여 기간에 배분할 입금"),node("p","급여에 해당하는 거래만 직접 선택하세요. 일부 금액만 해당하면 배분액을 수정하세요. 기본 상태에서는 어떤 거래도 급여로 확정하지 않습니다.","muted"));
    if(!materials.transactions?.length)target.append(node("p","추출한 입금 거래가 없습니다. 원문과 문서 유형을 확인해 주세요.","issue"));
    for(const [index,item] of (materials.transactions||[]).entries()){
      const row=node("div",undefined,"material-transaction"),checkLabel=node("label",undefined,"checkbox"),check=node("input"),principalLabel=node("label","원문 대조한 입금액 (원)","field"),principal=node("input"),amountLabel=node("label","이 기간에 배분할 금액 (원)","field"),amount=node("input"),saved=review.transactions?.[item.transaction_id];
      check.type="checkbox";check.checked=Boolean(saved?.selected)&&!hasNonAmountWarnings(item);check.disabled=Boolean(hasNonAmountWarnings(item));check.dataset.validationBlocked=String(Boolean(hasNonAmountWarnings(item)));check.id="transaction-select-"+index;checkLabel.append(check,node("span",(item.date||"날짜 미확인")+" · "+(item.description||"내용 미확인")+" · 거래금액 "+number(item.transaction_amount??item.amount)+"원"));
      principal.inputMode="decimal";principal.placeholder="미확인 · 원문 확인 후 입력";principal.id="transaction-principal-"+index;principal.value=saved&&Object.hasOwn(saved,"transaction_amount")?saved.transaction_amount??"":item.transaction_amount??"";principalLabel.htmlFor=principal.id;principalLabel.append(principal);
      amount.inputMode="decimal";amount.placeholder="미확인 · 원문 확인 후 입력";amount.id="transaction-amount-"+index;amount.value=saved&&Object.hasOwn(saved,"amount")?saved.amount??"":item.amount??item.transaction_amount??"";amountLabel.htmlFor=amount.id;amountLabel.append(amount);row.append(checkLabel,principalLabel,amountLabel,materialEvidenceButtons(item.evidence_refs,materials.evidence_catalog));appendMaterialWarnings(row,item,principal,()=>{check.checked=false;amount.value="";});if(hasNonAmountWarnings(item))row.append(node("p","거래일·거래 구분이 확인되지 않아 이 거래의 선택을 보류했습니다. 원문과 AI 처리 결과를 다시 확인하세요.","muted"));target.append(row);materialInputs.transactions.set(item.transaction_id,{selected:check,amount,transaction_amount:principal});
      if(item.duplicate_candidate_group)row.append(node("p","추출한 원문에서 중복 후보였던 거래입니다. 교정한 날짜·금액과 원문을 확인하세요.","issue"));
    }
    if(materials.transactions?.length>1||materials.transactions?.some(item=>item.duplicate_candidate_group)){
      const label=node("label",undefined,"checkbox"),input=node("input");input.type="checkbox";input.id="distinct-transactions-confirmed";input.checked=Boolean(review.distinct_transactions_confirmed);label.append(input,node("span","같은 날짜·금액의 중복 후보를 각각 별개의 거래로 확인했음"));distinctTransactionsInput=input;target.append(label,node("p","같은 중복 후보 그룹에서 한 건만 선택하면 이 확인은 필요하지 않습니다. 두 건 이상을 선택하려면 각각 다른 거래인지 확인해 주세요.","muted"));
    }
  }
  $("manual-section").open=true;
  updateStatementTotals();
}
function readMaterialReview() {
  return {earnings:Object.fromEntries([...materialInputs.earnings].map(([id,input])=>[id,input.value.trim()||null])),deductions:Object.fromEntries([...materialInputs.deductions].map(([id,input])=>[id,input.value.trim()||null])),row_roles:Object.fromEntries([...materialInputs.row_roles].map(([id,input])=>[id,input.value])),manual_rows:[...materialInputs.manual_rows].map(([row_id,inputs])=>({row_id,group:inputs.group,label:inputs.label.value.trim(),amount:inputs.amount.value.trim()||null,row_role:inputs.row_role.value,page:Number(inputs.page.value)})),transactions:Object.fromEntries([...materialInputs.transactions].map(([id,inputs])=>[id,{selected:inputs.selected.checked,amount:inputs.amount.value.trim()||null,transaction_amount:inputs.transaction_amount.value.trim()||null}])),distinct_transactions_confirmed:Boolean(distinctTransactionsInput?.checked)};
}
function duplicateTransactionReviewNeeded() {
  if(distinctTransactionsInput?.checked)return false;
  const groups=new Map();
  for(const item of current?.materials?.transactions||[]){
    const inputs=materialInputs.transactions.get(item.transaction_id),principal=inputs?.transaction_amount.value.trim();
    if(!inputs?.selected.checked||!item.date||!/^\d{1,13}(?:\.\d{1,2})?$/.test(principal||""))continue;
    const key=item.date+"|"+Number(principal),entries=groups.get(key)||[];
    for(const other of entries){
      if(item.account_number&&other.account_number){
        if(item.account_number!==other.account_number)continue;
        if(item.reference&&other.reference&&item.reference!==other.reference)continue;
      }
      return true;
    }
    entries.push(item);groups.set(key,entries);
  }
  return false;
}
function incompleteManualRows() {
  return [...materialInputs.manual_rows.values()].some(row=>!row.label.value.trim()||!Number.isInteger(Number(row.page.value))||Number(row.page.value)<1||Number(row.page.value)>Number(row.page.max));
}
function inputsConfirmed() {return Boolean(current&&!current.dirty&&!pendingDraft&&!current.materials_invalid&&current.confirmed_revision!=null);}
function comparisonProgressMessage() {
  if(isComparison())return current.materials_invalid?"원본이 변경되었습니다. 선택 문서를 다시 적용하고 확인해 주세요.":materialsSelectionPending()?"선택 문서를 적용해 주세요.":"선택 문서의 값과 거래를 확인한 뒤 ‘확인한 값으로 비교’를 누르세요.";
  if(materialsSelectionPending())return "선택한 자료를 먼저 불러온 뒤 입력값을 확인해 주세요.";
  if(current?.materials_invalid)return "연결 원본이 변경되었습니다. 선택 자료를 다시 불러와 주세요.";
  if(inputsConfirmed())return "입력값 확인 완료 · ‘자료 대조’를 눌러 선택한 자료를 비교하세요.";
  return current?.materials?.sources?.length?"자료 "+current.materials.sources.length+"건 연결 · 값 확인 대기 · ‘자료·값 확인’에서 입력값을 확인해 주세요.":"비교할 자료를 불러오고 입력값을 확인하면 대조를 시작할 수 있습니다.";
}
function renderComparisonGuidance() {
  const target=$("comparison-guidance"),materials=current?.materials;
  target.replaceChildren();target.hidden=!materials?.sources?.length;
  if(!target.hidden){
    target.append(node("strong","선택한 자료 "+materials.sources.length+"건의 값을 확인하세요."));
    const messages=[],hasDeposits=Boolean(materials.selection?.deposit_case_ids?.length),hasPayslip=Boolean(materials.selection?.payslip_case_id),hasAttendance=Boolean(materials.selection?.attendance_case_ids?.length);
    if(!$("period-start").value||!$("period-end").value)messages.push("대조할 급여 기간의 시작일과 종료일을 입력해 주세요.");
    if(!isComparison()&&($("manual-worked_minutes").value||$("manual-slip_minutes").value)&&!$("time-kind").value)messages.push("시간 비교 기준을 실제 근로시간 또는 유급 인정시간으로 확인해 주세요.");
    if(hasAttendance&&hasDeposits&&!hasPayslip&&!$("manual-net").value)messages.push("근무기록과 입금기록으로 예상 급여를 자동 계산하는 기능은 아직 없습니다. 입금액과 비교할 급여명세서나 직접 확인한 실수령액이 필요합니다.");
    else if(hasDeposits&&!$("manual-net").value)messages.push("입금액과 비교할 실수령액이 없습니다. 급여명세서를 연결하거나 원문에서 확인한 실수령액을 입력해 주세요.");
    if(hasDeposits&&(![...materialInputs.transactions.values()].some(row=>row.selected.checked)||!$("payment-confirmed").checked))messages.push("이번 급여에 해당하는 입금 거래를 선택하고 ‘이 기간의 입금 연결 범위를 확인했음’을 확인해 주세요.");
    if(incompleteManualRows())messages.push("수기로 보완한 항목의 항목명과 원문 페이지를 입력해 주세요.");
    for(const message of messages)target.append(node("p",message));
    target.append(node("p",isComparison()?"기간과 원문을 확인한 뒤 아래 ‘확인한 값으로 비교’를 누르세요. 빈값은 정보 부족으로 남습니다.":inputsConfirmed()?"입력값 확인이 끝났습니다. ‘대조 결과’에서 자료 대조를 실행하세요.":"기간과 값을 확인한 뒤 아래 ‘입력값 확인’을 누르세요. 값이 없는 비교 항목은 정보 부족으로 표시됩니다."));
  }
  if(!currentJob||["ocr","extract"].includes(currentJob.stage))$("job-status").textContent=comparisonProgressMessage();
}
function updateControls() {
  const running=busy()||changingType||loadingMaterials, invalid=Boolean(current?.materials_invalid), confirmed=inputsConfirmed();
  const selectionPending=materialsSelectionPending();
  const viewingHistory=Boolean(current?.analysis&&resultView().historical);
  const fresh=confirmed&&current.analysis&&!current.stale&&!selectionPending&&!viewingHistory;
  const duplicateReviewNeeded=duplicateTransactionReviewNeeded();
  const incompleteRows=incompleteManualRows();
  const extractionStale=Boolean(current?.extraction_needs_review&&(!current.materials?.sources?.length||current.materials.sources.some(source=>source.case_id===current.id)));
  $("ocr").disabled=!current||running;
  $("extract").disabled=!current?.ocr||running;
  $("confirm").disabled=!current||running||invalid||duplicateReviewNeeded||selectionPending||incompleteRows||extractionStale||!(current.extraction||current.materials?.sources?.length);
  $("confirmation-panel").hidden=!current;
  $("comparison-matrix").hidden=!isComparison();$("comparison-identity").hidden=!isComparison();$("confirm-compare").hidden=!isComparison();$("confirm").hidden=isComparison();$("analyze").hidden=comparisonMode||isComparison();$("time-kind").closest("label").hidden=isComparison();
  renderComparisonActionState(comparisonActionBlockers({running,invalid,selectionPending,duplicateReviewNeeded,incompleteRows}));
  $("document-type").disabled=!current||isComparison()||running;
  $("analyze").disabled=!confirmed||running||selectionPending||extractionStale; $("explain").disabled=!fresh||running; $("report").disabled=!fresh||!current.explanation||running;
  $("confirmation-state").textContent=extractionStale?"추출 검증 갱신 필요":duplicateReviewNeeded?"중복 거래 후보 확인 필요":incompleteRows?"보완 항목명·페이지 확인 필요":confirmed?"확인 완료 · v"+current.confirmed_revision:"사용자 확인 필요";
  $("confirmation-state").className="tag"+(confirmed?"":" warning");
  $("freshness").textContent=invalid?"연결 원본 변경 · 다시 불러오기 필요":current?.stale||pendingDraft?"입력 변경 · 갱신 필요":current?.analysis?"최신 확인값 결과":"결과 대기";
  $("freshness").className="tag"+(current?.stale||pendingDraft||invalid?" warning":"");
  $("load-materials").disabled=!current||running||comparisonSources===null;$("refresh-materials").disabled=!current||running;
  for(const [key,inputs] of Object.entries(materialSelectors)){for(const input of Array.isArray(inputs)?inputs:[inputs])input.disabled=running||(key.endsWith("_ids")&&!comparisonSources?.find(source=>source.case_id===input.value)?.has_extraction);}
  document.querySelectorAll("#confirmation-form input, #confirmation-form select").forEach(input=>{input.disabled=invalid||running||comparisonSubmitting||input.dataset.validationBlocked==="true";});
  document.querySelectorAll(".comparison-source-select input").forEach(input=>{input.disabled=running||comparisonSubmitting;});
  $("comparison-apply").disabled=running||comparisonSubmitting||!selectedComparisonSources().length;$("comparison-list").disabled=running||comparisonSubmitting;$("new-comparison").disabled=running||comparisonSubmitting;
  for(const [id,ready] of [["go-ai",Boolean(current?.ocr)],["go-compare",Boolean(current?.extraction)]])$(id).setAttribute("aria-disabled",String(!ready));
  renderComparisonGuidance();
  updateSupplementControls();
  if(typeof updatePageControls==="function")updatePageControls();
}
function openSourceDocument(documentId,title,page=1) {
  if(!documentId)return;
  if(comparisonMode||isComparison())showComparisonSource();
  if(!comparisonMode&&!isComparison()){$("workspace").hidden=false;if(activeRoute==="compare")$("workspace").classList.add("source-peek");}
  const url="/preview.html?document="+encodeURIComponent(documentId);
  $("pdf").src=url+"#page="+page;$("pdf").dataset.documentId=documentId;$("pdf").hidden=false;$("pdf-empty").hidden=true;
  $("document-title").textContent=title||docs.find(doc=>doc.id===documentId)?.title||"원문 PDF";$("open-pdf").href=url;$("open-pdf").hidden=false;$("page-info").textContent=(title||docs.find(doc=>doc.id===documentId)?.title||"원문 PDF")+" · "+page+"페이지";$("evidence-box").hidden=true;
}
function showEvidence(evidence,documentId) {
  if(comparisonMode||isComparison())showComparisonSource();else $("workspace").hidden=false;
  if(activeRoute==="compare")$("workspace").classList.add("source-peek");
  const source=current.materials?.sources?.find(item=>item.ref?.source_id===evidence.source_id);
  const resolvedId=documentId||source?.document_id||(evidence.source_id===current.document_id?current.document_id:null);
  if(resolvedId&&evidence.page)openSourceDocument(resolvedId,source?.title,evidence.page);
  $("evidence-title").textContent="원문 근거"+(evidence.page?" · "+evidence.page+"페이지":" · 사용자 확인값"); $("evidence-quote").textContent=evidence.quote||"별도의 원문 인용이 없는 확인값입니다.";
  $("evidence-method").textContent=evidence.verification_method==="manual_confirmed"?"사용자가 직접 확인한 값":evidence.verification_method==="ocr_unverified"?"근거: 로컬 Tesseract OCR 인식문 · 원본 일치 미검증 · 사용자 확인 필요":evidence.verification_method==="text_match"?"PDF 텍스트와 인용문 일치 · 내용의 정확성은 원본과 사용자 확인 필요":"검증 방법: 이미지 판독 · 독립 OCR 검증 없음 · 사용자 확인 필요";
  if(!resolvedId&&evidence.page)$("evidence-method").textContent+=" · 원본 연결 정보가 없어 인용문만 표시합니다.";
  $("evidence-box").hidden=false;
}
function readForm() {
  const result={values:Object.fromEntries(Object.keys(labels).map(k=>[k,$("value-"+k).value.trim()||null])),
    contract_unit:$("contract-unit").value,slip_unit:$("slip-unit").value,contract_period_confirmed:$("contract-period-confirmed").checked,period_start:$("period-start").value,period_end:$("period-end").value,
    manual:Object.fromEntries(Object.keys(manualLabels).map(k=>[k,$("manual-"+k).value.trim()||null])),time_kind:$("time-kind").value,
    earnings_complete:$("earnings-complete").checked,deductions_complete:$("deductions-complete").checked,payment_scope_confirmed:$("payment-confirmed").checked,material_review:readMaterialReview()};
  if(isComparison()){result.worked_time_kind=$("worked-time-kind").value;result.slip_time_kind=$("slip-time-kind").value;result.subjects_confirmed=$("same-subject-confirmed").checked;result.source_identity=readSourceIdentity();result.time_kind="";}
  if(typeof readWageForm==="function")Object.assign(result,readWageForm());
  return result;
}
function changed() { if(!current)return; pendingDraft=true; current.dirty=true; current.stale=Boolean(current.analysis); updateStatementTotals(); updateComparisonCompatibility();renderExtractionChanges(); updateControls(); renderResults(); clearTimeout(draftTimer); draftTimer=setTimeout(()=>flushDraft().catch(e=>notice(e.message,true)),400); }
async function flushDraft() {
  clearTimeout(draftTimer); if(!current||!pendingDraft) return saveChain;
  const id=current.id, resource=caseApiPath(),draft=readForm(); pendingDraft=false;
  saveChain=saveChain.catch(()=>{}).then(async()=>{const saved=await api(resource+"/draft","PUT",draft); if(current?.id===id){current=saved;syncActiveRecord();updateComparisonCompatibility();updateControls();renderResults();}});
  try {await saveChain;} catch(error) {pendingDraft=true;updateControls();throw error;}
}
async function start(stage) {
  if(!current)throw new Error("먼저 PDF를 업로드하거나 선택해 주세요.");
  if(stage==="extract"&&!current.ocr)throw new Error("원문 읽기 페이지에서 먼저 PDF를 읽어 주세요.");
  if(stage==="report"&&typeof startReport==="function")return startReport();
  await flushDraft(); notice("");
  if(stage==="analyze"){routeResultId="";resultVersionId="";}
  if(stage==="analyze")switchSection("results");
  currentJob=await api(caseApiPath()+"/jobs","POST",{stage,include_original:$("include-original").checked});
  renderJob(); pollJob();
}
function renderJob() {
  const supplement=currentJob?.stage==="supplement",extraction=["extract","supplement"].includes(currentJob?.stage),ocr=currentJob?.stage==="ocr",resumable=currentJob&&["failed","cancelled","interrupted"].includes(currentJob.status);
  $("extraction-job-status").hidden=!extraction;
  $("ocr-job-status").hidden=!ocr;
  $("extraction-cancel").textContent=supplement?"PDF 보완 읽기 취소":"AI 처리 취소";$("extraction-resume").textContent=supplement?"PDF 보완 읽기 다시 실행":"AI 처리 다시 실행";
  for(const [prefix,visible] of [["",!extraction&&!ocr],["extraction-",extraction],["ocr-",ocr]]){$(prefix+"cancel").hidden=!(visible&&busy());$(prefix+"resume").hidden=!(visible&&resumable);}
  if(!currentJob){$("job-status").textContent=comparisonProgressMessage();$("extraction-job-status").textContent="";$("ocr-job-status").textContent="";return updateControls();}
  const statusLabels={queued:"접수됨",running:"실행 중",completed:"완료",failed:"실패",cancelled:"취소됨",interrupted:"중단됨",superseded:"이전 입력 결과 보류"};
  const last=currentJob.events?.at(-1);
  const phaseLabels={inspect_document:supplement?"선택 항목의 원본 PDF 준비":extraction?"저장 원문 불러오기":"PDF 텍스트·스캔 문자 읽기",extract_candidates:supplement?"선택 항목 보완 읽기":"AI 문서·항목 정리",verify_candidates:supplement?"보완 후보 형식·출처 확인":"원문·값 검증",package_candidates:"후보 저장"};
  $(ocr?"ocr-job-status":extraction?"extraction-job-status":"job-status").textContent=(stageLabels[currentJob.stage]||"")+" · "+(supplement&&currentJob.status==="completed"?"보완 읽기 완료 · A·B값 확인 필요":extraction&&currentJob.status==="completed"?"항목 정리 완료 · "+extractionReviewCount(current?.extraction)+"개 확인 필요":statusLabels[currentJob.status]||currentJob.status)+((extraction||ocr)&&last?" / "+(phaseLabels[last.node_id]||"처리 중"):last?" / "+currentJob.events.length+"개 단계 진행":"")+(currentJob.error?" · 처리를 마치지 못했습니다. 개발 정보에서 상세 내용을 확인하세요.":"");
  updateControls();if(typeof renderDevelopmentInfo==="function")renderDevelopmentInfo();
}
async function pollJob() {
  clearTimeout(pollTimer); if(!currentJob) return;
  const id=currentJob.id;
  try {
    const job=await api("/api/jobs/"+id); if(currentJob?.id!==id)return; currentJob=job;renderJob();
    if(busy()) pollTimer=setTimeout(pollJob,800);
    else {await flushDraft();current=await api(caseApiPath());syncActiveRecord();renderCase();renderJob();if(job.stage==="extract"&&job.status==="completed")await loadComparisonSources();}
  } catch(error) {notice("상태 조회가 중단됐습니다. 새로고침하면 저장한 작업을 다시 확인합니다. "+error.message,true);}
}
function renderResults() { renderComparisonResults(); updateControls(); if(typeof renderWorkPages==="function")renderWorkPages(); }

function updateRouteLinks() {
  document.querySelectorAll("a[data-route]").forEach(link=>{link.href=routeUrl(link.dataset.route);if(link.closest(".workflow-nav"))link.setAttribute("aria-current",link.dataset.route===activeRoute?"page":"false");});
}
function renderRoute() {
  const info=routeInfo[activeRoute];
  for(const route of Object.keys(routeInfo))$(route+"-page").hidden=route!==activeRoute;
  $("workspace").hidden=!["ocr","ai"].includes(activeRoute);$("workspace").classList.remove("source-peek");
  $("document-library").hidden=!["ocr","ai"].includes(activeRoute);
  $("page-title").textContent=info.title;$("page-description").textContent=info.description;$("page-eyebrow").textContent=info.eyebrow;$("page-step").textContent=info.eyebrow;
  document.title="월급잇다 · "+info.title;document.body.dataset.route=activeRoute;updateRouteLinks();
  if(typeof document.querySelector==="function"){
    if(["ocr","ai"].includes(activeRoute)){$("workspace").insertBefore($("source-panel"),$("workspace").firstChild);closeComparisonSource();}
    $("comparison-picker").hidden=activeRoute!=="compare";
  }
  if(typeof renderWorkPages==="function")renderWorkPages();
}
async function navigateRoute(route,push=true) {
  if(loadingMaterials||comparisonSubmitting||candidateRequestPending||!Object.hasOwn(routeInfo,route))return;
  const requestedUrl=routeUrl(route);
  await flushDraft();if(typeof flushInquiry==="function")await flushInquiry();syncActiveRecord();clearTimeout(pollTimer);currentJob=null;activeRoute=route;
  if(payrollRoute(route)&&!comparisonCase&&!routeComparisonId&&documentCase?.analysis){comparisonMode=false;current=documentCase;renderCase();await restoreCurrentJob();}
  else if(payrollRoute(route)){
    comparisonMode=true;current=comparisonCase;
    await Promise.all([loadComparisons(),loadComparisonSources()]);
    if(!current){const saved=routeComparisonId||localStorage.getItem("agent-last-comparison");if(saved&&comparisonRecords.some(item=>item.id===saved))await openComparison(saved);else{current=null;renderComparisonPicker();renderResults();}}
    else{renderCase();await restoreCurrentJob();}
  }else if(route!=="glossary"){
    comparisonMode=false;current=documentCase;
    await loadDocuments();
    if(!current){const saved=localStorage.getItem("agent-last-case");if(saved){try{const restored=await api("/api/cases/"+saved);if(docs.some(doc=>doc.id===restored.document_id))current=documentCase=restored;}catch{localStorage.removeItem("agent-last-case");}}}
    if(current){renderCase();await restoreCurrentJob();}else{renderOcr();updateControls();}
  }
  if(push)history.pushState(null,"",route==="glossary"?requestedUrl:routeUrl(route));renderRoute();notice("");
  if(route==="glossary"&&typeof loadGlossary==="function")await loadGlossary();
}
async function restoreHistoryRoute() {
  if(loadingMaterials||comparisonSubmitting||candidateRequestPending){history.replaceState(null,"",routeUrl(activeRoute));return;}
  await flushDraft();if(typeof flushInquiry==="function")await flushInquiry();
  const params=new URLSearchParams(location.search),route=location.pathname.split("/")[1];
  routeResultId=params.get("result")||"";resultVersionId=routeResultId;routeComparisonId=params.get("comparison")||"";
  activeRoute=Object.hasOwn(routeInfo,route)?route:"ocr";
  if(routeComparisonId&&comparisonCase?.id!==routeComparisonId)comparisonCase=await api("/api/comparisons/"+encodeURIComponent(routeComparisonId));
  if(!routeComparisonId&&payrollRoute(activeRoute))comparisonCase=null;
  const caseId=params.get("case");if(caseId&&documentCase?.id!==caseId)documentCase=await api("/api/cases/"+encodeURIComponent(caseId));
  current=payrollRoute(activeRoute)?comparisonCase||documentCase:activeRoute==="glossary"?current:documentCase;
  await navigateRoute(activeRoute,false);
}
function switchSection(section) {
  activeRoute=section==="workspace"?"compare":section;
  if(!Object.hasOwn(routeInfo,activeRoute))activeRoute="ocr";
  history.pushState(null,"",routeUrl(activeRoute));renderRoute();
}
function showConfirmation() {
  switchSection("compare");$("manual-section").open=true;
  const heading=$("confirmation-heading");if(typeof heading.focus==="function")heading.focus({preventScroll:true});if(typeof heading.scrollIntoView==="function")heading.scrollIntoView({block:"start",behavior:"auto"});
}
function populateDocumentTypes(select) {
  const selected=select.value==="payslip_ocr"?"payslip":select.value;
  select.replaceChildren();
  const choices=Object.entries(profiles).filter(([key])=>key!=="payslip_ocr").sort(([left],[right])=>left==="auto"?-1:right==="auto"?1:0);
  for(const [key,profile] of choices){const option=node("option",profile.label);option.value=key;select.append(option);}
  select.value=Object.hasOwn(profiles,selected)?selected:Object.hasOwn(profiles,"auto")?"auto":choices[0]?.[0]||"";
}
async function initialize() {
  renderRoute();
  profiles=await api("/api/extraction-profiles");populateDocumentTypes($("document-type"));buildFields();
  if(typeof initializeWorkPages==="function")initializeWorkPages();
  document.querySelectorAll("a[data-route]").forEach(link=>link.addEventListener("click",safeAction(async event=>{event.preventDefault();if(link.getAttribute("aria-disabled")==="true")return;await navigateRoute(link.dataset.route);})));
  window.addEventListener("popstate",safeAction(restoreHistoryRoute));
  $("refresh-materials").addEventListener("click",safeAction(loadComparisonSources));
  $("load-materials").addEventListener("click",safeAction(applyMaterials));
  $("document-type").addEventListener("change",safeAction(async()=>{
    const selected=$("document-type").value;changingType=true;updateControls();
    try{if(current.materials_invalid){clearTimeout(draftTimer);pendingDraft=false;}else await flushDraft();current=await api("/api/cases/"+current.id+"/document-type","PUT",{document_type:selected});clearTimeout(pollTimer);currentJob=null;renderCase();await loadComparisonSources();notice("문서 유형을 "+profiles[selected].label+"로 변경했습니다. AI 처리를 실행해 주세요.");}
    catch(error){$("document-type").value=current.document_type==="payslip_ocr"?"payslip":current.document_type;throw error;}
    finally{changingType=false;updateControls();}
  }));
  for(const stage of ["ocr","extract","analyze","explain","report"])$(stage).addEventListener("click",safeAction(()=>start(stage)));
  $("supplement").addEventListener("click",safeAction(startSupplement));
  $("confirmation-form").addEventListener("input",changed);
  $("confirmation-form").addEventListener("change",()=>{if(pendingDraft)flushDraft().catch(error=>notice(error.message,true));});
  $("confirmation-form").addEventListener("submit",safeAction(async event=>{event.preventDefault();if(isComparison()){if(!$("confirm-compare").disabled)await confirmAndCompare();return;}await flushDraft();current=await api(caseApiPath()+"/confirm","POST",{});renderCase();switchSection("results");notice("입력값을 확인했습니다. 자료 대조 버튼을 눌러 결과를 확인하세요.");}));
  for(const prefix of ["","extraction-","ocr-"]){
    $(prefix+"cancel").addEventListener("click",safeAction(async()=>{currentJob=await api("/api/jobs/"+currentJob.id+"/cancel","POST",{});renderJob();await pollJob();}));
    $(prefix+"resume").addEventListener("click",safeAction(async()=>{await flushDraft();currentJob=await api("/api/jobs/"+currentJob.id+"/resume","POST",{});renderJob();pollJob();}));
  }
  $("upload").addEventListener("change",safeAction(async event=>{const file=event.target.files[0];if(!file)return;await flushDraft();if(file.size>20*1024*1024)throw new Error("20MB 이하 PDF를 선택해 주세요.");const response=await fetch("/api/uploads?document_type=auto",{method:"POST",headers:{"Content-Type":"application/pdf","X-Local-Agent":"1","X-Document-Name":encodeURIComponent(file.name)},body:file});const result=await response.json();if(!response.ok)throw new Error(result.error);current=result;currentJob=null;clearTimeout(pollTimer);await loadDocuments();await navigateRoute("ocr");rememberCase();renderCase();await loadComparisonSources();notice("PDF를 업로드했습니다. OCR 추출을 실행해 주세요.");event.target.value="";}));
  const health=await api("/api/health");$("connection").textContent="로컬 작업실 연결됨";if(typeof setDevelopmentHealth==="function")setDevelopmentHealth(health);
  if(activeRoute==="glossary"&&typeof loadGlossary==="function"){await loadGlossary();renderRoute();return;}
  await loadDocuments();
  if(await initializeComparisons())return;
  const saved=new URLSearchParams(location.search).get("case")||localStorage.getItem("agent-last-case");
  if(saved){try{const restored=await api("/api/cases/"+saved);if(docs.some(doc=>doc.id===restored.document_id)){current=restored;rememberCase();renderCase();await restoreCurrentJob();return;}}catch{/* Removed records do not prevent a fresh upload. */}if(new URLSearchParams(location.search).get("case")){current=documentCase=null;renderRoute();notice("주소의 문서를 찾을 수 없습니다. 다른 문서로 자동 이동하지 않았습니다.",true);return;}localStorage.removeItem("agent-last-case");history.replaceState(null,"",routeUrl(activeRoute));}
  if(docs[0])await selectDocument(docs[0].id);else{renderOcr();renderMaterials();renderResults();updateControls();}
}
initialize().catch(error=>{$("connection").textContent="연결 확인 필요";notice(error.message,true);});
