"use strict";
// Presentation only: all comparison values and differences come from the Python result.
const resultStatusNames={match:"자료상 일치",difference:"차이 발견",insufficient:"정보 부족",needs_review:"검토 필요"};
const resultReasonNames={FIELD_MISSING:"비교할 확인값이 없습니다.",FIELD_UNKNOWN:"해당 값을 원문에서 확인해 주세요.",FIELD_PENDING:"아직 확인하지 않은 값이 있습니다.",FIELD_CONFLICT:"자료의 값이나 적용 범위가 서로 다릅니다.",UNIT_MISMATCH:"단위가 달라 바로 비교할 수 없습니다.",PERIOD_MISMATCH:"자료의 적용기간이 대조 기간과 다릅니다.",TIME_MEANING_UNCONFIRMED:"실근로시간과 유급시간 중 비교 기준을 확인해 주세요.",WORK_SOURCE_CONFLICT:"겹치는 근무기록을 확인해 주세요.",ITEM_SCOPE_INCOMPLETE:"지급·공제 세부 항목이 모두 확인되지 않았습니다.",PAYMENT_SCOPE_UNCONFIRMED:"해당 급여에 연결할 입금과 배분액을 확인해 주세요.",DUPLICATE_STATEMENT_ITEM:"중복된 명세서 항목을 확인해 주세요.",DUPLICATE_TRANSACTION:"같은 거래가 중복 연결되어 있습니다.",INVALID_ALLOCATION:"거래 금액과 배분액을 확인해 주세요."};
Object.assign(resultReasonNames,{WAGE_COMPONENT_DIFFERENCE:"총액은 같을 수 있지만 계산한 임금과 급여명세서의 항목별 금액에 차이가 있습니다.",SOURCE_PERIOD_MISMATCH:"근거 문서에 연결한 적용기간이 이 급여 기간을 포함하지 않습니다. 문서와 적용기간을 다시 확인해 주세요.",WORK_SUMMARY_MISMATCH:"날짜별 근무기록의 실제시간 합계와 확인한 근태 총시간이 다릅니다. 상세 기록과 총시간을 대조해 주세요.",WORK_BREAK_UNCONFIRMED:"휴게시간이 겹치거나 실제 위치가 확인되지 않아 근무시간을 확정할 수 없습니다."});
resultReasonNames.HOLIDAY_BOUNDARY_REVIEW="자정을 넘는 근무 중 휴일의 구분이 달라져 원문 기록과 적용 기준을 확인해 주세요.";
const resultRules={
  "R-01":{title:"계약 단가 대조",types:["contract","payslip"],formula:"명세서 단가 − 계약서 단가",baseline:"rhs"},
  "R-02":{title:"근무시간 대조",types:["attendance","payslip"],formula:"명세서 시간 − 근무기록 시간",baseline:"rhs"},
  "R-03":{title:"지급 항목 합계",types:["payslip","payslip"],formula:"확인한 지급 항목 합계 − 명세서 총지급액",baseline:"lhs"},
  "R-04":{title:"공제 항목 합계",types:["payslip","payslip"],formula:"확인한 공제 항목 합계 − 명세서 공제 합계",baseline:"lhs"},
  "R-05":{title:"실수령액 산식",types:["payslip","payslip"],formula:"(총지급액 − 총공제액) − 명세서 실수령액",baseline:"lhs"},
  "R-07":{title:"계산 임금 · 급여명세서",types:["contract","payslip"],formula:"같은 기간·항목·포함 범위의 계산액 − 명세서 금액",baseline:"lhs"},
  "R-06":{title:"실수령액 · 입금 대조",types:["payslip","deposit"],formula:"명세서 실수령액 − 선택한 입금의 배분액 합계",baseline:"lhs"},
};
const resultScopes={all:{label:"전체 대조 항목",rules:Object.keys(resultRules)},terms:{label:"계약·계산 ↔ 급여명세서",rules:["R-01","R-07"]},time:{label:"근무기록 ↔ 급여명세서",rules:["R-02"]},statement:{label:"급여명세서 내부 확인",rules:["R-03","R-04","R-05"]},payment:{label:"급여명세서 ↔ 입금기록",rules:["R-06"]}};
let resultStatusFilter="all",resultScopeFilter="all",resultVersionId="",resultCaseId=null,resultLatestId=null;

function resultView() {
  const history=current?.analysis_history||[];
  if(typeof routeResultId!=="undefined")resultVersionId=routeResultId;
  const selected=resultVersionId?history.find(entry=>entry.analysis?.result_id===resultVersionId):null;
  if(resultVersionId&&!selected&&resultVersionId!==current?.analysis?.result_id)return {analysis:null,historical:true,stale:true,invalidVersion:true};
  const entry=selected||{analysis:current?.analysis,period:current?.analysis_period,materials:current?.analysis_materials,snapshot:current?.analysis_snapshot};
  const historical=Boolean(selected&&selected.analysis.result_id!==current?.analysis?.result_id);
  return {...entry,historical,stale:!historical&&Boolean(current?.stale||current?.dirty||pendingDraft||current?.materials_invalid)};
}
function resultSources(view) {return view.materials?.sources||(!view.stale&&!view.historical?current?.confirmed_materials?.sources||current?.materials?.sources:[])||[];}
function resultSourceForEvidence(view,evidence) {
  return resultSources(view).find(source=>source.ref?.source_id===evidence.source_id)||
    view.snapshot?.authorized_sources?.filter(source=>source.kind==="document").map(source=>({document_id:source.document_id,title:docs.find(doc=>doc.id===source.document_id)?.title||"원본 PDF",ref:source})).find(source=>source.ref.source_id===evidence.source_id)||
    docs.filter(doc=>"source-"+doc.id===evidence.source_id).map(doc=>({document_id:doc.id,title:doc.title}))[0];
}
function resultTypeSources(view,type) {return resultSources(view).filter(source=>source.document_type===type||(type==="payslip"&&source.document_type==="payslip_ocr"));}
function resultValue(value,unit) {
  if(value===null||value===undefined||String(value).trim()==="")return "확인 필요";
  const suffix={KRW:"원","KRW/hour":"원 / 시간","KRW/month":"원 / 월","KRW/day":"원 / 일","KRW/year":"원 / 년",minute:"분"}[unit]||unit||"";
  return number(value)+(suffix==="원"||suffix==="분"?"":" ")+suffix;
}
function resultDifference(item) {
  if(item.status==="match")return "차이 없음";
  if(item.status!=="difference"||item.delta===null||item.delta===undefined)return "확인 후 비교";
  const delta=Number(item.delta);if(!Number.isFinite(delta))return "확인 필요";
  if(item.rule_id==="R-07"&&delta===0&&(item.reason_codes||[]).includes("WAGE_COMPONENT_DIFFERENCE"))return "총액 차이 0원 · 항목별 차이 있음";
  const amount=resultValue(Math.abs(delta),item.unit);
  if(item.rule_id==="R-06")return "입금액이 "+amount+(delta>0?" 적음":" 많음");
  if(item.rule_id==="R-01")return "명세서 단가가 "+amount+(delta<0?" 낮음":" 높음");
  if(item.rule_id==="R-02")return "명세서 시간이 "+amount+(delta<0?" 적음":" 많음");
  return amount+" 차이";
}
function resultBadge(status) {return node("span",resultStatusNames[status]||"검토 필요","result-badge "+status);}
function resultPeriod(view) {return (view.period?.start||"기간 미확인")+" ~ "+(view.period?.end||"기간 미확인");}
function resultFiles(view,type) {return resultTypeSources(view,type).map(source=>source.title||"원본 PDF").join(" · ")||"연결 자료 없음 · 직접 확인값 확인";}
function resultEvidenceButton(view,evidence) {
  const source=resultSourceForEvidence(view,evidence);
  const label=source?.title|| (evidence.verification_method==="manual_confirmed"?"직접 확인한 값":"원문 인용");
  const button=node("button",label+(evidence.page?" · "+evidence.page+"쪽 ↗":" ↗"),"result-source-button");button.type="button";
  button.addEventListener("click",()=>{closeResultDetail();switchSection("workspace");showEvidence(evidence,source?.document_id);if(typeof $("evidence-box").scrollIntoView==="function")$("evidence-box").scrollIntoView({behavior:"smooth",block:"center"});});
  return button;
}
function closeResultDetail() {
  const dialog=$("comparison-detail");if(typeof dialog.close==="function"&&dialog.open)dialog.close();dialog.hidden=true;
}
function showResultDetail(item,view) {
  const body=$("comparison-detail-body"),rule=resultRules[item.rule_id];body.replaceChildren();
  const header=node("div",undefined,"result-detail-header"),title=node("h2",item.rule_id+" · "+rule.title);title.id="comparison-detail-title";
  const close=node("button","✕","result-detail-close");close.type="button";close.setAttribute("aria-label","결과 상세 닫기");close.addEventListener("click",closeResultDetail);header.append(title,close);body.append(header);
  const content=node("div",undefined,"result-detail-content");content.append(resultBadge(item.status));
  if(view.stale||view.historical)content.append(node("p",view.historical?"이전 실행의 결과와 근거입니다.":"입력이 바뀐 이전 결과입니다. 최신 자료로 다시 대조해 주세요.","result-stale"));
  const values=node("div",undefined,"result-detail-values"),sides=comparisonSides[item.rule_id];
  for(const [index,key] of ["lhs","rhs"].entries()){const value=node("div");value.append(node("small",sides[index]),node("strong",resultValue(item[key],item.unit)));values.append(value);}
  content.append(values,node("div",resultDifference(item),"result-detail-difference "+item.status));
  const reasons=[...new Set(item.reason_codes||[])];
  content.append(node("p",item.status==="difference"?"확인한 자료에 숫자 차이가 있습니다. 원문과 추가 지급 내역을 함께 확인해 주세요.":item.status==="match"?"현재 확인한 자료의 범위에서 값이 같습니다.":"비교를 완료하려면 아래 자료와 확인 기준을 보완해 주세요.","result-detail-explanation"));
  for(const reason of reasons)content.append(node("p",resultReasonNames[reason]||"원문과 비교 기준을 확인해 주세요.","result-reason"));
  const basis=node("section",undefined,"result-basis");basis.append(node("h3","비교 기준"),node("p",rule.formula),node("p","급여 기간: "+resultPeriod(view)),node("p","확인 입력 v"+(view.analysis.input_version??"—")+" · "+resultTimestamp(view.analysis.created_at)));
  content.append(basis);
  if(item.rule_id==="R-07")for(const part of item.wage_items||[]){const row=node("section",undefined,"wage-result");row.append(node("strong",part.rule_ids.map(id=>typeof wageNames!=="undefined"?wageNames[id]||id:id).join(" + ")),resultBadge(part.status),node("p","계산액 "+resultValue(part.lhs,"KRW")+" · 명세서 "+resultValue(part.rhs,"KRW")),node("p",part.independent?"확인한 계약·근무 조건으로 계산":"명세서에서 가져온 기준에 의존한 계산 · 독립 검증 아님","muted"));content.append(row);}
  content.append(node("h3","사용한 원문 근거"));
  const catalog=new Map((view.analysis.evidence_catalog||[]).map(evidence=>[evidence.evidence_id,evidence]));
  let count=0;
  for(const id of new Set(item.evidence_refs||[])){const evidence=catalog.get(id);if(!evidence)continue;count++;const source=node("section",undefined,"result-evidence-card");source.append(resultEvidenceButton(view,evidence),node("blockquote",evidence.quote||"사용자가 입력하고 확인한 값"));if(["vision_unverified","ocr_unverified"].includes(evidence.verification_method))source.append(node("small","문자 인식 결과 · 원본과 직접 대조해 주세요."));content.append(source);}
  if(!count)content.append(node("p","이 항목의 원문 근거가 없습니다. 비교할 문서를 선택하고 해당 값을 확인해 주세요.","muted"));
  const revise=node("button","자료·확인값 보완하기","primary");revise.type="button";revise.addEventListener("click",()=>{closeResultDetail();switchSection("workspace");});content.append(revise);body.append(content);
  const dialog=$("comparison-detail");dialog.hidden=false;if(typeof dialog.showModal==="function"&&!dialog.open)dialog.showModal();
}
function resultTimestamp(value) {if(!value)return "실행 시각 미확인";const date=new Date(value);return Number.isNaN(date.getTime())?"실행 시각 미확인":date.toLocaleString("ko-KR",{timeZone:"Asia/Seoul",month:"numeric",day:"numeric",hour:"2-digit",minute:"2-digit"});}

function renderComparisonResults() {
  for(const id of ["comparison-results","result-overview","result-metrics","result-filters","result-support-note","downloads","questions","explanation"])$(id).replaceChildren();
  $("explanation").hidden=true;
  if(resultCaseId!==current?.id){resultCaseId=current?.id;resultStatusFilter="all";resultScopeFilter="all";resultVersionId=typeof routeResultId!=="undefined"?routeResultId:"";resultLatestId=current?.analysis?.result_id;closeResultDetail();}
  if(resultLatestId!==current?.analysis?.result_id){resultLatestId=current?.analysis?.result_id;resultVersionId=typeof routeResultId!=="undefined"?routeResultId:"";closeResultDetail();}
  const scope=$("result-scope");scope.replaceChildren();for(const [key,definition] of Object.entries(resultScopes)){const option=node("option",definition.label);option.value=key;scope.append(option);}scope.value=resultScopeFilter;scope.onchange=()=>{resultScopeFilter=scope.value;resultStatusFilter="all";renderResults();};
  const versions=$("result-version");versions.replaceChildren();const latest=node("option",current?.analysis?"최신 결과 · "+resultTimestamp(current.analysis.created_at):"아직 대조하지 않음");latest.value="";versions.append(latest);
  for(const entry of [...(current?.analysis_history||[])].reverse()){if(entry.analysis?.result_id===current?.analysis?.result_id)continue;const option=node("option","이전 결과 · "+resultTimestamp(entry.analysis?.created_at)+" · 입력 v"+entry.analysis?.input_version);option.value=entry.analysis.result_id;versions.append(option);}versions.value=resultVersionId;versions.disabled=versions.children.length<2;versions.onchange=()=>{resultVersionId=versions.value;routeResultId=resultVersionId;history.replaceState(null,"",routeUrl(activeRoute));updateRouteLinks();resultStatusFilter="all";renderResults();};
  const view=resultView();
  if(view.invalidVersion){$("comparison-results").append(node("p","요청한 결과 버전을 찾을 수 없습니다.","result-stale"));return;}
  if(!view.analysis){
    if(typeof isComparison==="function"&&(isComparison()||comparisonMode)){
      const count=current?.materials?.sources?.length||0,empty=node("div",undefined,"result-empty");empty.append(node("span","↔","result-empty-icon"),node("h3",count?"문서 "+count+"건의 값을 확인해 주세요.":"어떤 문서를 비교할까요?"),node("p",count?"같은 항목을 나란히 확인하고 ‘확인한 값으로 비교’를 누르면 이곳에 결과가 표시됩니다.":"AI 처리 완료 문서를 선택한 뒤 같은 대상과 기간의 자료인지 확인하세요."));if(count){const button=node("button","비교할 값 확인으로 이동");button.type="button";button.addEventListener("click",showConfirmation);empty.append(button);}$("comparison-results").append(empty);return;
    }
    const count=current?.materials?.sources?.length||0,ready=inputsConfirmed()&&!materialsSelectionPending(),invalid=current?.materials_invalid||materialsSelectionPending();
    const empty=node("div",undefined,"result-empty");empty.append(node("span","↔","result-empty-icon"),node("h3",invalid?"선택한 자료를 불러와 주세요.":ready?"대조할 준비가 됐습니다.":count?"자료 "+count+"건을 불러왔습니다. 값을 확인해 주세요.":"어떤 자료를 비교할까요?"),node("p",invalid?"위에서 ‘선택 자료 불러오기’를 눌러 변경한 자료를 적용하세요.":ready?"‘자료 대조’를 누르면 확인한 값으로 항목별 차이를 계산합니다.":count?"기간·금액·입금 거래를 확인하고 ‘입력값 확인’을 눌러 주세요.":"위에서 대조할 파일을 선택하고 불러온 뒤, 기간과 값을 확인해 주세요."));
    const button=node("button",ready?"자료 대조 시작":"대조할 값 확인으로 이동","primary");button.type="button";button.disabled=ready&&Boolean(busy()||loadingMaterials||changingType);button.addEventListener("click",ready?safeAction(()=>start("analyze")):showConfirmation);empty.append(button);$("comparison-results").append(empty);return;
  }
  const entries=(view.analysis.comparisons||[]).filter(item=>resultRules[item.rule_id]);
  const scoped=entries.filter(item=>resultScopes[resultScopeFilter].rules.includes(item.rule_id));
  const heading=node("div",undefined,"result-context"),intro=node("div");intro.append(node("span",view.historical?"이전 실행 결과":view.stale?"이전 입력의 결과":"확인한 자료의 대조 결과","eyebrow"),node("h3",resultPeriod(view)),node("p","확인 입력 v"+(view.analysis.input_version??"—")+" · "+resultTimestamp(view.analysis.created_at)));heading.append(intro);
  const sources=node("div",undefined,"result-context-sources");for(const source of resultSources(view)){const chip=node("span",source.title||"원본 PDF","result-file-chip");sources.append(chip);}if(!sources.children.length)sources.append(node("span","직접 확인한 값 기준","result-file-chip"));heading.append(sources);$("result-overview").append(heading);
  if(view.stale||view.historical)$("result-overview").append(node("p",view.historical?"이전 결과입니다. 당시 선택한 문서와 확인값을 표시합니다.":"자료나 입력값이 바뀌었습니다. 아래는 이전 결과이며 다시 확인·대조해야 합니다.","result-stale"));
  const counts=Object.fromEntries(Object.keys(resultStatusNames).map(status=>[status,scoped.filter(item=>item.status===status).length]));
  const hints={match:"확인한 자료의 값이 같아요",difference:"차이와 원문을 확인해 보세요",insufficient:"추가 자료나 확인값이 필요해요",needs_review:"기간·단위·적용 범위를 확인해요"};
  for(const [status,label] of Object.entries(resultStatusNames)){const button=node("button",undefined,"result-metric "+status+(resultStatusFilter===status?" selected":""));button.type="button";button.setAttribute("aria-pressed",String(resultStatusFilter===status));button.append(node("span",label),node("strong",String(counts[status])+"건"),node("small",hints[status]));button.addEventListener("click",()=>{resultStatusFilter=resultStatusFilter===status?"all":status;renderResults();});$("result-metrics").append(button);}
  for(const [status,label] of [["all","전체"],...Object.entries(resultStatusNames)]){const button=node("button",label+" "+(status==="all"?scoped.length:counts[status]),"result-filter"+(resultStatusFilter===status?" selected":""));button.type="button";button.setAttribute("aria-pressed",String(resultStatusFilter===status));button.addEventListener("click",()=>{resultStatusFilter=status;renderResults();});$("result-filters").append(button);}
  const scroll=node("div",undefined,"result-table-scroll"),table=node("table",undefined,"result-table");table.append(node("caption","선택한 급여 자료의 항목별 대조 결과"));const head=node("thead"),header=node("tr");for(const text of ["확인 항목","비교 기준 · 자료","대조 값 · 자료","결과","확인된 차이",""]){const th=node("th",text);th.setAttribute("scope","col");header.append(th);}head.append(header);table.append(head);const body=node("tbody");
  for(const item of scoped.filter(item=>resultStatusFilter==="all"||item.status===resultStatusFilter)){const definition=resultRules[item.rule_id],row=node("tr");const title=node("td");title.append(node("strong",definition.title),node("small",item.rule_id));row.append(title);const keys=definition.baseline==="rhs"?["rhs","lhs"]:["lhs","rhs"];
    for(const [index,key] of keys.entries()){const cell=node("td");const label=comparisonSides[item.rule_id][key==="lhs"?0:1];cell.append(node("small",label+":"),node("strong",resultValue(item[key],item.unit)),node("small",resultFiles(view,definition.types[index]),"result-cell-source"));row.append(cell);}
    const status=node("td");status.append(resultBadge(item.status));row.append(status,node("td",resultDifference(item),"result-delta "+item.status));const action=node("td"),button=node("button","근거 · 상세 보기 ↗","result-detail-button");button.type="button";button.setAttribute("aria-label",definition.title+" 근거와 상세 보기");button.addEventListener("click",()=>showResultDetail(item,view));action.append(button);row.append(action);body.append(row);}
  if(!body.children.length){const row=node("tr"),cell=node("td","이 조건에 해당하는 결과가 없습니다.","result-no-rows");cell.colSpan=6;row.append(cell);body.append(row);}table.append(body);scroll.append(table);$("comparison-results").append(scroll);

  if(view.stale||view.historical)return;
  if(current.explanation){$("explanation").hidden=false;$("explanation").append(node("strong","대조 결과 설명"));for(const text of current.explanation.paragraphs)$("explanation").append(node("p",text));}
  if(view.analysis.questions?.length){const details=node("details");details.append(node("summary","추가로 확인할 내용 "+view.analysis.questions.length+"건"));for(const question of view.analysis.questions)details.append(node("p",question.text));$("questions").append(details);}
  for(const artifact of current.report?.artifacts||[]){const link=node("a",artifact.mime_type==="application/pdf"?"보고서 PDF 다운로드 ↓":"자료 묶음 ZIP 다운로드 ↓","download-link");link.href="/api/artifacts/"+artifact.artifact_id;link.download="";$("downloads").append(link);}
}
