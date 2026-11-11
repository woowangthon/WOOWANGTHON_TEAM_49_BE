"use strict";
// A comparison owns its selection and review; document OCR/AI state stays separate.
let documentCase=null, comparisonCase=null, comparisonRecords=[], comparisonMode=false;
let comparisonSelection={contract_case_id:null,attendance_case_ids:[],payslip_case_id:null,deposit_case_ids:[]};
let comparisonSubmitting=false;
let identityInputs=new Map();
function isComparison(record=current) {return record?.kind==="comparison";}
function caseApiPath(record=current) {return (isComparison(record)?"/api/comparisons/":"/api/cases/")+record.id;}
function syncActiveRecord() {if(isComparison())comparisonCase=current;else if(current)documentCase=current;}
function rememberComparison() {if(!isComparison())return;comparisonCase=current;localStorage.setItem("agent-last-comparison",current.id);routeComparisonId=current.id;history.replaceState(null,"",routeUrl(activeRoute));updateRouteLinks();}
function emptyComparisonSelection() {return {contract_case_id:null,attendance_case_ids:[],payslip_case_id:null,deposit_case_ids:[]};}
function selectedComparisonSources() {const ids=new Set(Object.values(comparisonSelection).flat().filter(Boolean));return (comparisonSources||[]).filter(source=>ids.has(source.case_id));}
function sameComparisonSelection(left,right) {return Object.keys(emptyComparisonSelection()).every(key=>Array.isArray(left[key])?JSON.stringify([...left[key]].sort())===JSON.stringify([...(right?.[key]||[])].sort()):(left[key]||null)===(right?.[key]||null));}
async function loadComparisons() {comparisonRecords=(await api("/api/comparisons")).comparisons;renderComparisonList();}
function renderComparisonList() {
  const select=$("comparison-list");select.replaceChildren();const empty=node("option","새 비교 만들기");empty.value="";select.append(empty);
  for(const item of comparisonRecords){const option=node("option",(item.title||"문서 비교")+(item.draft?.period_start?" · "+item.draft.period_start:""));option.value=item.id;select.append(option);}select.value=comparisonCase?.id||"";
}
async function openComparison(id) {
  if(id&&id!==routeComparisonId){routeResultId="";resultVersionId="";}
  routeComparisonId=id||"";
  if(loadingMaterials||comparisonSubmitting||candidateRequestPending)return;
  await flushDraft();syncActiveRecord();clearTimeout(pollTimer);currentJob=null;
  comparisonMode=true;
  if(id){current=comparisonCase=await api("/api/comparisons/"+encodeURIComponent(id));comparisonSelection=structuredClone(current.materials?.selection||emptyComparisonSelection());rememberComparison();renderCase();await restoreCurrentJob();}
  else{current=comparisonCase=null;comparisonSelection=emptyComparisonSelection();localStorage.removeItem("agent-last-comparison");history.replaceState(null,"",routeUrl(activeRoute));$("comparison-title").value="";renderComparisonPicker();renderResults();updateControls();}
  renderComparisonList();renderRoute();
}
function comparisonKind(source) {return source.document_type==="payslip_ocr"?"payslip":source.document_type;}
function sourceSummary(source) {return source.summary||source.key_values||{};}
function compactTime(value) {if(value==null||value==="")return "미확인";const minutes=Number(value);return Number.isFinite(minutes)?(minutes%60===0?minutes/60+"시간":Math.floor(minutes/60)+"시간 "+minutes%60+"분"):String(value);}
function summaryValue(summary,key) {const value=summary[key];return value&&typeof value==="object"?value.value??value.normalized_value:value;}
function sourceCardSummary(source) {
  const summary=sourceSummary(source),kind=comparisonKind(source),rows=[];
  if(kind==="contract")rows.push(["임금",resultValue(summaryValue(summary,"contract_rate"),summaryValue(summary,"contract_unit"))]);
  if(kind==="attendance")rows.push([summaryValue(summary,"worked_time_kind")==="paid"?"유급 인정시간":"근무시간",compactTime(summaryValue(summary,"worked_minutes"))]);
  if(kind==="payslip")rows.push(["단가",resultValue(summaryValue(summary,"slip_rate"),summaryValue(summary,"slip_unit"))],["총지급",resultValue(summaryValue(summary,"gross"),"KRW")],["실수령",resultValue(summaryValue(summary,"net"),"KRW")]);
  if(kind==="deposit")rows.push(["입금합계",resultValue(summaryValue(summary,"deposit_total"),"KRW")]);
  return rows;
}
function sourceIdentityText(source) {const identity=source.identity||{};return [identity.employee_name,identity.employer_name].filter(Boolean).join(" · ")||"대상 정보 미확인";}
function selectComparisonSource(source,selected) {
  const key={contract:"contract_case_id",attendance:"attendance_case_ids",payslip:"payslip_case_id",deposit:"deposit_case_ids"}[comparisonKind(source)];if(!key)return;
  if(Array.isArray(comparisonSelection[key]))comparisonSelection[key]=selected?[...new Set([...comparisonSelection[key],source.case_id])]:comparisonSelection[key].filter(id=>id!==source.case_id);
  else comparisonSelection[key]=selected?source.case_id:null;
  renderComparisonPicker();renderResults();updateControls();
}
function normalizedIdentity(value) {return String(value||"").normalize("NFC").replace(/\s/g,"").toLocaleLowerCase().replace(/ß/g,"ss").replace(/ς/g,"σ");}
function comparisonSelectionWarnings(sources=selectedComparisonSources()) {
  const messages=[],known=(key)=>[...new Set(sources.map(source=>normalizedIdentity(source.identity?.[key])).filter(Boolean))];
  if(known("employee_name").length>1)messages.push("근로자 이름이 다른 문서가 선택되었습니다. 같은 근로자의 자료인지 원문을 확인하세요.");
  if(known("employer_name").length>1)messages.push("사업장 이름이 다른 문서가 선택되었습니다. 다른 사업장의 자료는 별도 비교로 나누세요.");
  const periods=[...new Set(sources.filter(source=>source.period_start&&source.period_end&&["attendance","payslip"].includes(comparisonKind(source))).map(source=>source.period_start+" ~ "+source.period_end))];
  if(periods.length>1)messages.push("선택한 문서의 대상 기간이 다릅니다. 비교할 급여 기간을 먼저 확인하세요.");
  const contract=sources.find(source=>comparisonKind(source)==="contract"),slip=sources.find(source=>comparisonKind(source)==="payslip");
  const contractUnit=contract&&summaryValue(sourceSummary(contract),"contract_unit"),slipUnit=slip&&summaryValue(sourceSummary(slip),"slip_unit");
  if(contractUnit&&slipUnit&&contractUnit!==slipUnit)messages.push("계약서와 명세서의 임금 단위가 다릅니다. 시급과 월급을 자동 환산하지 않습니다.");
  if(sources.length&&sources.some(source=>!source.identity?.employee_name||!source.identity?.employer_name))messages.push("대상 정보가 확인되지 않은 문서가 있습니다. 원문에서 같은 근로자·사업장인지 확인해 주세요.");
  return messages;
}
function renderComparisonPicker() {
  if(!comparisonMode&&!isComparison())return;
  const target=$("comparison-cards");target.replaceChildren();
  const ready=(comparisonSources||[]).filter(source=>source.has_extraction);
  if(!ready.length)target.append(node("p",comparisonSources===null?"AI 처리된 자료를 불러오는 중입니다.":"AI 처리 완료 문서가 없습니다. AI 처리 페이지에서 항목 정리를 완료해 주세요.","stage-empty"));
  const selectedIds=new Set(Object.values(comparisonSelection).flat().filter(Boolean));
  for(const source of ready){
    const card=node("article",undefined,"comparison-source-card"+(selectedIds.has(source.case_id)?" selected":"")),label=node("label",undefined,"comparison-source-select"),check=node("input");check.type="checkbox";check.checked=selectedIds.has(source.case_id);check.disabled=busy()||loadingMaterials;check.addEventListener("change",()=>selectComparisonSource(source,check.checked));label.append(check,node("strong",source.title||"문서"));card.append(label);
    const start=comparisonKind(source)==="contract"?source.contract_start:source.period_start,end=comparisonKind(source)==="contract"?source.contract_end:source.period_end;
    card.append(node("span",profiles[comparisonKind(source)]?.label||"자료","source-type"),node("p",sourceIdentityText(source),"source-identity"),node("p",(start||"기간 미확인")+(end?" ~ "+end:""),"source-period"));
    const summary=node("dl",undefined,"source-card-values");for(const [name,value] of sourceCardSummary(source))summary.append(node("dt",name),node("dd",value));card.append(summary);
    const count=source.review_needed_count??source.needs_review_count??null,badges=node("div",undefined,"source-card-badges");badges.append(node("span","AI 정리 완료","validation-status"));badges.append(node("span",count==null?"사용자 확인 전":count?"확인 필요 "+count+"개":"사용자 확인 전",count?"validation-status needs_review":"validation-status missing"));card.append(badges);
    const preview=node("button","원문 보기 ↗","evidence-link");preview.type="button";preview.addEventListener("click",()=>openSourceDocument(source.document_id,source.title));card.append(preview);target.append(card);
    if(count>0)card.append(supplementSourceLink(source));
  }
  const selected=selectedComparisonSources(),tray=$("comparison-selection-tray");tray.replaceChildren(node("strong",selected.length+"개 문서 선택"));for(const source of selected)tray.append(node("span",(profiles[comparisonKind(source)]?.label||"자료")+" · "+source.title,"selected-source-chip"));
  $("comparison-selection-warnings").replaceChildren(...comparisonSelectionWarnings(selected).map(message=>node("p",message,"issue")));
  $("comparison-apply").textContent=isComparison()?"선택 문서 적용":"선택한 문서 확인";$("comparison-apply").disabled=!selected.length||busy()||loadingMaterials;
  $("comparison-title").value=isComparison()?current.title||"":$("comparison-title").value;$("comparison-title").disabled=isComparison();
  $("comparison-review-state").textContent=isComparison()?(inputsConfirmed()?"이번 비교 입력 확인 완료":"이번 비교 입력 확인 전"):"문서를 고르면 같은 항목을 나란히 확인할 수 있습니다.";
  $("materials-panel").hidden=true;$("comparison-picker").hidden=false;
}
function supplementSourceLink(source) {const link=node("a","미확인 항목 보완 →","supplement-follow-link");link.href="/ai?case="+encodeURIComponent(source.case_id)+(comparisonCase?"&comparison="+encodeURIComponent(comparisonCase.id):"")+(routeResultId?"&result="+encodeURIComponent(routeResultId):"");link.addEventListener("click",safeAction(async event=>{event.preventDefault();if(busy()||loadingMaterials||comparisonSubmitting)return;await flushDraft();syncActiveRecord();const record=await api("/api/cases/"+encodeURIComponent(source.case_id));clearTimeout(pollTimer);currentJob=null;current=documentCase=record;activeRoute="ai";comparisonMode=false;rememberCase();renderCase();renderRoute();await restoreCurrentJob();}));return link;}
async function applyComparisonSelection() {
  if(loadingMaterials||comparisonSubmitting)return;
  if(!selectedComparisonSources().length)return;
  await flushDraft();loadingMaterials=true;updateControls();
  try{const selection=structuredClone(comparisonSelection);if(isComparison())current=comparisonCase=await api(caseApiPath()+"/materials","PUT",selection);else current=comparisonCase=await api("/api/comparisons","POST",{title:$("comparison-title").value.trim()||"문서 비교",selection});rememberComparison();renderCase();await loadComparisons();showConfirmation();notice("선택한 문서의 값을 나란히 확인하세요. 원문을 확인한 뒤 ‘확인한 값으로 비교’를 누르세요.");}
  finally{loadingMaterials=false;updateControls();}
}
function comparisonSourceNames(kind) {return (current?.materials?.sources||[]).filter(source=>comparisonKind(source)===kind).map(source=>source.title).join(" · ")||"연결한 자료 없음";}
function renderComparisonContext() {
  if(!isComparison())return;
  $("comparison-title").value=current.title||"";
  for(const [id,kind] of [["rate-contract-source","contract"],["rate-slip-source","payslip"],["time-work-source","attendance"],["time-slip-source","payslip"],["statement-source","payslip"],["payment-slip-source","payslip"],["payment-bank-source","deposit"]])$(id).textContent=comparisonSourceNames(kind);
  $("worked-time-kind").value=current.draft.worked_time_kind||"";$("slip-time-kind").value=current.draft.slip_time_kind||"";
  $("same-subject-confirmed").checked=Boolean(current.draft.subjects_confirmed);
  const payslip=(current.materials?.sources||[]).find(source=>source.case_id===current.materials?.selection?.payslip_case_id)||(comparisonSources||[]).find(source=>source.case_id===current.materials?.selection?.payslip_case_id),paid=summaryValue(sourceSummary(payslip||{}),"slip_paid_minutes");
  $("slip-paid-reference").textContent=paid!=null?"참고 · 명세서 유급 인정시간 "+compactTime(paid)+". 위의 비교 시간과 다른 항목입니다.":"유급 인정시간과 실제 근로시간이 따로 기재되어 있다면 각각의 의미를 확인하세요.";
  renderIdentityInputs();
  updateComparisonCompatibility();
}
function renderIdentityInputs() {
  const target=$("comparison-identity-fields");target.replaceChildren();identityInputs=new Map();
  for(const source of current.materials?.sources||[]){
    const identity={...(source.identity||{}),...Object.fromEntries(Object.entries(current.draft.source_identity?.[source.case_id]||{}).filter(([,value])=>value))};
    const row=node("div",undefined,"comparison-identity-row"),heading=node("div");heading.append(node("strong",source.title));const original=node("button","원문 확인 ↗","evidence-link");original.type="button";original.addEventListener("click",()=>openSourceDocument(source.document_id,source.title));heading.append(original);row.append(heading);
    const fields={};for(const [key,label] of [["employee_name","근로자 이름"],["employee_id","사번 (있는 경우)"],["employer_name","사업장 이름"]]){const wrap=node("label",label,"field"),input=node("input"),candidate=node("small",undefined,"muted");input.value=identity[key]||"";input.placeholder="원문에서 확인";input.maxLength=120;input.setAttribute("aria-label",source.title+" "+label);const showCandidate=()=>{const original=source.identity?.[key]||"";candidate.textContent=normalizedIdentity(input.value)!==normalizedIdentity(original)?"원문 후보: "+(original||"미확인")+" · 이번 비교에서 보완":"";};showCandidate();input.addEventListener("input",()=>{$("same-subject-confirmed").checked=false;showCandidate();});wrap.append(input,candidate);row.append(wrap);fields[key]=input;}identityInputs.set(source.case_id,fields);target.append(row);
  }
}
function readResolvedSourceIdentity() {return Object.fromEntries([...identityInputs].map(([id,fields])=>[id,Object.fromEntries(Object.entries(fields).map(([key,input])=>[key,input.value.trim()]))]));}
function readSourceIdentity() {const overrides={};for(const [id,identity] of Object.entries(readResolvedSourceIdentity())){const original=(current.materials?.sources||[]).find(source=>source.case_id===id)?.identity||{},changed=Object.fromEntries(Object.entries(identity).filter(([key,value])=>normalizedIdentity(value)!==normalizedIdentity(original[key])));if(Object.keys(changed).length)overrides[id]=changed;}return overrides;}
function comparisonHasBlockingIssues() {
  if(!isComparison())return false;
  const identities=Object.values(readResolvedSourceIdentity());
  if(["employee_name","employee_id","employer_name"].some(key=>new Set(identities.map(value=>normalizedIdentity(value[key])).filter(Boolean)).size>1))return true;
  return !pendingDraft&&Boolean(current.compatibility?.blocked);
}
function comparisonActionBlockers({running,invalid,selectionPending,duplicateReviewNeeded,incompleteRows}) {
  if(!isComparison())return [];
  const messages=[];
  if(running||comparisonSubmitting)messages.push("자료를 처리하고 있습니다. 처리가 끝나면 비교할 수 있습니다.");
  if(invalid)messages.push("연결한 원본이 변경되었습니다. 선택한 문서를 다시 적용해 주세요.");
  if(selectionPending)messages.push("문서 선택이 변경되었습니다. 먼저 ‘선택 문서 적용’을 눌러 주세요.");
  if(!current.materials?.sources?.length)messages.push("대조할 문서를 선택하고 적용해 주세요.");
  if(duplicateReviewNeeded)messages.push("중복으로 보이는 입금 거래가 있습니다. 거래 목록에서 중복 여부를 확인해 주세요.");
  if(incompleteRows)messages.push("직접 추가한 항목의 이름과 원문 페이지를 확인해 주세요.");
  if(!$("same-subject-confirmed").checked)messages.push("같은 근로자·사업장에 관한 자료인지 원문에서 확인한 뒤 체크해 주세요.");
  if(comparisonHasBlockingIssues()){
    const identities=Object.values(readResolvedSourceIdentity());let identityConflict=false;
    for(const [key,label] of [["employee_name","근로자 이름"],["employee_id","사번"],["employer_name","사업장 이름"]]){
      if(new Set(identities.map(value=>normalizedIdentity(value[key])).filter(Boolean)).size>1){identityConflict=true;messages.push(label+"이 서로 다릅니다. ‘같은 근로자·사업장의 자료인가요?’에서 원문에 맞게 수정해 주세요.");}
    }
    if(!pendingDraft&&current.compatibility?.blocked){
      const issues=(current.compatibility.issues||[]).filter(issue=>(issue.blocking||issue.severity==="blocking")&&(!identityConflict||issue.code!=="SUBJECT_MISMATCH"));
      for(const issue of issues)if(issue.message)messages.push(issue.message);
      if(!identityConflict&&!issues.some(issue=>issue.message))messages.push("문서의 대상 정보나 기간이 맞지 않습니다. 원문과 비교 조건을 확인해 주세요.");
    }
  }
  if(comparisonTimeBasisMissing())messages.push("입력한 시간의 기준을 선택해 주세요. 확인하기 어려운 시간은 빈값으로 보류할 수 있습니다.");
  return [...new Set(messages)];
}
function renderComparisonActionState(blockers) {
  for(const suffix of ["","-top"]){
    $("comparison-actions"+suffix).hidden=!isComparison();
    const button=$("confirm-compare"+suffix),status=$("comparison-action-status"+suffix);
    button.hidden=!isComparison();button.disabled=!isComparison()||blockers.length>0;
    status.replaceChildren();
    if(blockers.length){status.append(node("strong","비교 전 확인할 내용"));const list=node("ul");for(const message of blockers)list.append(node("li",message));status.append(list);}
    else status.append(node("p","원문과 입력값을 확인했다면 비교할 수 있습니다. 빈 항목은 정보 부족으로 표시됩니다."));
  }
}
function updateComparisonCompatibility() {
  if(!isComparison())return;
  const editedIdentity=readResolvedSourceIdentity(),messages=comparisonSelectionWarnings((current.materials?.sources||[]).map(source=>({...((comparisonSources||[]).find(item=>item.case_id===source.case_id)||{}),...source,identity:editedIdentity[source.case_id]||source.identity})));
  const contractUnit=$("contract-unit").value,slipUnit=$("slip-unit").value;
  $("rate-compatibility").textContent=contractUnit&&slipUnit&&contractUnit!==slipUnit?"단위가 다릅니다. 각 원문의 금액과 단위를 유지하고, 이 항목은 검토 필요로 남깁니다.":"각 문서에 적힌 금액과 단위를 원문에서 확인하세요.";
  const work=$("worked-time-kind").value,slip=$("slip-time-kind").value;
  $("time-compatibility").textContent=comparisonTimeBasisMissing()?"입력한 시간마다 실제 근로시간 또는 유급 인정시간을 선택해 주세요. 기준을 확인하기 어려우면 해당 시간을 빈값으로 보류하세요.":work&&slip&&work!==slip?"실제 근로시간과 유급 인정시간은 서로 다른 기준입니다. 이 두 값의 차이를 미지급 시간으로 판단하지 않습니다.":"두 문서의 시간 기준을 각각 확인하세요. 소정근로시간을 실제 근로시간으로 바꾸지 않습니다.";
  $("worked-time-display").textContent=compactTime($("manual-worked_minutes").value);$("slip-time-display").textContent=compactTime($("manual-slip_minutes").value);
  let deposit=0n,count=0,missing=0;for(const row of materialInputs.transactions.values()){if(!row.selected.checked)continue;count++;const value=statementCents(row.amount.value.trim());if(value===null)missing++;else deposit+=value;}
  $("selected-deposit-total").textContent=count?(missing?"선택 거래에 미확인 금액 있음":statementAmount(deposit)):"급여에 해당하는 거래를 선택하세요";$("selected-deposit-count").textContent="직접 선택한 거래 "+count+"건";
  const identities=Object.values(editedIdentity);for(const [key,label] of [["employee_name","근로자 이름"],["employee_id","사번"],["employer_name","사업장 이름"]]){if(new Set(identities.map(value=>normalizedIdentity(value[key])).filter(Boolean)).size>1)messages.push(label+"이 일치하지 않습니다. 다른 대상의 문서를 제외하거나 원문에서 확인한 정보로 바로잡아 주세요.");}
  if(!pendingDraft)for(const issue of current.compatibility?.issues||[])if(issue.message)messages.push(issue.message);
  $("comparison-identity-warnings").replaceChildren(...[...new Set(messages)].map(message=>node("p",message,"issue")));
}
function comparisonTimeBasisMissing() {return isComparison()&&Boolean($("manual-worked_minutes").value&&!$("worked-time-kind").value||$("manual-slip_minutes").value&&!$("slip-time-kind").value);}
async function confirmAndCompare() {
  if(!isComparison()||comparisonSubmitting||loadingMaterials||$("confirm-compare").disabled)return;
  comparisonSubmitting=true;updateControls();
  try{await flushDraft();current=comparisonCase=await api(caseApiPath()+"/confirm","POST",{});renderCase();await start("analyze");}
  finally{comparisonSubmitting=false;updateControls();}
}
function setupComparisonLayout() {
  if(typeof document.querySelector!=="function")return;
  const move=(inputId,destination)=>{const input=$(inputId),wrap=input?.closest(".field");if(wrap)$(destination).append(wrap);};
  for(const [id,target] of [["value-contract_rate","rate-contract-fields"],["contract-unit","rate-contract-fields"],["manual-slip_rate","rate-slip-fields"],["slip-unit","rate-slip-fields"],["manual-worked_minutes","time-work-fields"],["manual-slip_minutes","time-slip-fields"],["manual-gross","statement-fields"],["manual-deduction","statement-fields"],["manual-net","payment-slip-fields"]])move(id,target);
}
function showComparisonSource() {
  if(!comparisonMode&&!isComparison())return;
  $("comparison-source-dock").hidden=false;$("comparison-source-dock").classList.add("open");$("comparison-source-dock").append($("source-panel"));$("close-comparison-source").hidden=false;
}
function closeComparisonSource() {$("comparison-source-dock").classList.remove("open");$("comparison-source-dock").hidden=true;}
async function initializeComparisons() {
  setupComparisonLayout();
  $("comparison-list").addEventListener("change",safeAction(event=>openComparison(event.target.value)));
  $("new-comparison").addEventListener("click",safeAction(()=>openComparison(null)));
  $("comparison-apply").addEventListener("click",safeAction(applyComparisonSelection));
  for(const id of ["confirm-compare","confirm-compare-top"])$(id).addEventListener("click",safeAction(confirmAndCompare));
  $("close-comparison-source").addEventListener("click",closeComparisonSource);
  for(const id of ["worked-time-kind","slip-time-kind","same-subject-confirmed"])$(id).addEventListener("change",()=>{changed();});
  if(payrollRoute(activeRoute)&&new URLSearchParams(location.search).get("case")&&!new URLSearchParams(location.search).get("comparison"))return false;
  if(payrollRoute(activeRoute)){comparisonMode=true;await Promise.all([loadComparisons(),loadComparisonSources()]);const saved=new URLSearchParams(location.search).get("comparison")||localStorage.getItem("agent-last-comparison");if(saved&&comparisonRecords.some(record=>record.id===saved))await openComparison(saved);else if(saved){current=comparisonCase=null;renderResults();notice("주소의 급여 건을 찾을 수 없습니다. 다른 급여 건으로 자동 이동하지 않았습니다.",true);renderRoute();}else await openComparison(null);return true;}
  return false;
}
