"use strict";
// These pages display stored facts and submit explicit user actions. They never call a model.
const wageNames={"W-01":"기본급","W-02":"주휴수당","W-03":"연장수당","W-04":"야간수당","W-05":"휴일수당"};
const stateNames={missing:"미입력",unknown:"모름",pending:"확인 예정",conflict:"자료 간 충돌",known:"값을 확인함"};
const boolConditions={standard_employment:"일반 고정 근무제에 해당함",contract_unchanged:"이 급여기간에 계약 조건이 바뀌지 않음",five_or_more_workers:"상시 근로자 5명 이상인 사업장",full_period_employment:"급여기간 전체에 재직함",records_complete:"필요한 날짜의 근무기록을 모두 확인함",composition_complete:"임금 구성과 포함 수당을 모두 확인함",monthly_weekly_rest_included:"월급에 주휴분이 포함되어 있음"};
const numberConditions={ordinary_hourly_rate:"확인한 통상시급 (원)",ordinary_monthly_amount:"통상임금에 포함되는 월 금액 (원)",monthly_standard_minutes:"월 통상임금 산정 기준시간 (분)"};
const weekdays=["월","화","수","목","금","토","일"];
let wageFormCase=null,wageControls={},calendarControls=[],workControls=[],coverageControls=[],allowanceControls=[],mappingControls=[];
let glossaryEntries=[],glossaryLoaded=false,glossaryLoading=null,developmentHealth={};
let inquiryKey="",inquiryUnsaved=false,inquiryTimer=null,inquirySaving=Promise.resolve(),inquiryRecord=null;
let reportSelectionKey="",reportSourceInputs=new Map(),questionSelectionKey="",inquiryQuestionInputs=new Map();
let localRowSequence=0;
function control(tag,label,value="",type="text") {const wrap=node("label",label,"field"),input=node(tag);if(tag==="input")input.type=type;input.value=value??"";input.setAttribute("aria-label",label);wrap.append(input);return {wrap,input};}
function choice(label,choices,value="") {const item=control("select",label);for(const [key,text] of choices){const option=node("option",text);option.value=key;item.input.append(option);}item.input.value=String(value??"");return item;}
function button(text,handler,className="") {const item=node("button",text,className);item.type="button";item.addEventListener("click",safeAction(handler));return item;}
function pageLink(route,label) {const link=node("a",label,"button-link");link.href=routeUrl(route);link.addEventListener("click",safeAction(async event=>{event.preventDefault();await navigateRoute(route);}));return link;}
function stateChoice(value,label="확인 상태") {return choice(label,Object.entries(stateNames),value||"missing");}
function boolChoice(label,value) {return choice(label,[["missing","미입력"],["unknown","모름"],["pending","확인 예정"],["conflict","자료 간 충돌"],["yes","예 · 확인함"],["no","아니요 · 확인함"]],value?.state==="known"?(value.value?"yes":"no"):value?.state||"missing");}
function conditionValue(input,value,previous={}) {const state=input.value;if(state!=="known")return {...previous,state,value:null};if(value===""||value==null)throw new Error("‘값을 확인함’을 선택한 계산 조건의 값을 입력해 주세요.");return {...previous,state,value};}
function setDevelopmentHealth(value) {developmentHealth=value;renderDevelopmentInfo();}
function renderDevelopmentInfo() {$("development-details").textContent=JSON.stringify({model:developmentHealth.model,case_id:current?.id,result_id:resultView().analysis?.result_id,extraction_version:current?.extraction?.extraction_version,engine_version:resultView().analysis?.engine_version,job:currentJob?{id:currentJob.id,stage:currentJob.stage,status:currentJob.status,usage:currentJob.usage,error_code:currentJob.error_code,error:currentJob.error}:null},null,2);}
function workPageContext() {
  const target=$("work-context");target.replaceChildren();
  if(["ocr","ai"].includes(activeRoute)){target.append(node("strong",docForCase()?.title||"선택한 문서 없음"));if(comparisonCase||routeComparisonId)target.append(pageLink("compare","연결했던 급여 건으로 돌아가기"));return;}
  if(activeRoute==="glossary")return;
  if(!current){target.append(node("p","먼저 급여 건을 선택해 주세요."),pageLink("compare","급여 건 선택"));return;}
  const view=resultView(),sources=resultSources(view),names=[...new Set(sources.map(source=>source.identity?.employer_name).filter(Boolean))];
  target.append(node("strong",current.title||"급여 확인"),node("span",names.join(" · ")||"사업장 미확인"),node("span",view.analysis?resultPeriod(view):(current.draft?.period_start||"시작일 미확인")+" ~ "+(current.draft?.period_end||"종료일 미확인")));
  if(view.analysis)target.append(node("span","확인 입력 v"+view.analysis.input_version,"tag"));
  if(view.invalidVersion)target.append(node("p","주소의 결과 버전을 찾을 수 없습니다. 최신 결과로 자동 대체하지 않았습니다.","result-stale"));
  else if(view.historical||view.stale)target.append(node("p",view.historical?"과거 결과를 보고 있습니다. 현재 입력과 다를 수 있습니다.":"입력이 변경되었습니다. 결과·질문·문의·보고서는 이전 입력 기준입니다.","result-stale"));
  if(activeRoute!=="compare")target.append(pageLink("compare","자료·조건 보완"));
}
function renderWorkPages() {
  workPageContext();renderDevelopmentInfo();renderWageResults();
  if(activeRoute==="questions")renderQuestionPage();
  if(activeRoute==="inquiry")renderInquiryPage();
  if(activeRoute==="reports")renderReportPage();
  if(activeRoute==="glossary")renderGlossary();
  updatePageControls();
}
function workCandidateTable(target,workRows) {
  if(!workRows.length)return;
  const list=node("div",undefined,"work-candidates");list.append(node("h4","연결한 근무기록의 후보"),node("p","불러온 뒤 실제 날짜·시각·휴게를 확인하세요. 종료시각이 이르거나 같으면 자정 근무 여부를 직접 입력합니다.","muted"));
  for(const candidate of workRows){const row=node("section",undefined,"work-candidate"),valid=/^\d{4}-\d\d-\d\d$/.test(candidate.date||"")&&/^\d\d:\d\d$/.test(candidate.start||"")&&/^\d\d:\d\d$/.test(candidate.end||"")&&candidate.end>candidate.start&&!(candidate.validation_warnings||[]).length;row.append(node("p",[candidate.date||"날짜 미확인",candidate.start||"시작 미확인","~",candidate.end||"종료 미확인",candidate.break_interval||"휴게 위치 확인 필요"].join(" ")));
    const source=current.materials?.sources?.find(item=>item.case_id===candidate.source_case_id);if(source)row.append(button((source.title||"근무기록")+" 원문 보기",()=>openSourceDocument(source.document_id,source.title)));
    if(valid)row.append(button("이 행을 확인 입력으로 불러오기",()=>{if(workControls.some(item=>item.id===candidate.record_id)){notice("이미 불러온 근무 행입니다.");return;}addWorkRow(target,{record_id:candidate.record_id,start_at:candidate.date+"T"+candidate.start+":00+09:00",end_at:candidate.date+"T"+candidate.end+":00+09:00",evidence_refs:candidate.evidence_refs||[],breaks:[],break_location_known:false});changed();notice("근무시각 후보를 불러왔습니다. 휴게시간과 원문을 확인해 주세요.");}));else row.append(node("p","시각 또는 날짜 확인 필요 · 아래에서 근무기록을 직접 추가하세요.","muted"));list.append(row);
  }
  target.before(list);
}
function renderWageForm() {
  const target=$("wage-conditions"),hasContract=Boolean(current?.materials?.selection?.contract_case_id||current?.document_type==="contract");
  target.hidden=!hasContract&&!current?.draft?.wage_conditions;wageFormCase=current?.id;
  target.replaceChildren();wageControls={};calendarControls=[];workControls=[];coverageControls=[];allowanceControls=[];mappingControls=[];
  if(target.hidden)return;
  const enabled=node("input");enabled.type="checkbox";enabled.id="wage-enabled";enabled.checked=Boolean(current.draft.wage_conditions);const label=node("label",undefined,"checkbox");label.append(enabled,node("span","연결한 계약과 실제 근무기록으로 임금도 계산합니다."));target.append(node("h3","임금 계산 조건"),label,node("p","명세서와 입금만 대조할 때는 입력하지 않아도 됩니다. 모르는 값은 그대로 두면 필요한 계산 항목만 보류됩니다.","muted"));wageControls.enabled=enabled;
  const body=node("div");body.id="wage-condition-body";body.hidden=!enabled.checked;target.append(body);enabled.addEventListener("change",()=>{body.hidden=!enabled.checked;});
  const existing=current.draft.wage_conditions||{},grid=node("div",undefined,"condition-grid");
  for(const [key,text] of Object.entries(boolConditions)){const item=boolChoice(text,existing[key]);item.input.id="condition-"+key;item.input.dataset.conditionField=key;wageControls[key]=item.input;grid.append(item.wrap);}
  body.append(node("p","첫 지원 범위는 성인의 1일 8시간·주 40시간 일반 고정 근무입니다. 단시간·특수 근무제·월 중 입퇴사 안분·기간 중 계약 변경은 별도 확인이 필요합니다.","muted"),grid,node("p","‘아니요’와 ‘모름’은 다릅니다. 지원 범위를 벗어난 조건은 금액으로 추정하지 않습니다.","muted"));
  for(const [key,text] of Object.entries(numberConditions)){const row=node("div",undefined,"field-pair"),value=control("input",text,existing[key]?.value,"number"),state=stateChoice(existing[key]?.state);value.input.min="0";value.input.step=key==="monthly_standard_minutes"?"1":"0.01";value.input.id="condition-"+key;wageControls[key]={input:value.input,state:state.input};row.append(value.wrap,state.wrap);body.append(row);}
  body.append(node("p","월 기준시간은 계약의 유급시간과 임금 구성을 확인해 입력하세요. 209시간을 자동 적용하지 않습니다.","muted"),glossaryLink("ordinary_wage","통상임금이란?"));
  const schedule=node("details"),scheduleGrid=node("div",undefined,"condition-grid"),stored=existing.schedule?.value||{};schedule.append(node("summary","약속한 주 근무일정"));const scheduleState=stateChoice(existing.schedule?.state,"주 일정 확인 상태");schedule.append(scheduleState.wrap);wageControls.schedule={state:scheduleState.input,days:[]};
  for(let day=0;day<7;day++){const item=control("input",weekdays[day]+"요일 소정근로 (분)",stored.days?.find(entry=>entry.weekday===day)?.minutes,"number");item.input.min="1";item.input.max="480";item.input.step="1";item.input.placeholder="근무 없는 요일은 빈칸";wageControls.schedule.days.push(item.input);scheduleGrid.append(item.wrap);}
  for(const [key,text] of [["week_start","주 계산 시작 요일"],["weekly_rest_weekday","주휴일 요일"]]){const item=choice(text,[["","확인 필요"],...weekdays.map((name,i)=>[String(i),name+"요일"])],stored[key]);wageControls.schedule[key]=item.input;scheduleGrid.append(item.wrap);}
  const rest=control("input","주휴 유급 인정시간 (분)",stored.weekly_rest_minutes,"number");rest.input.min="0";rest.input.max="480";wageControls.schedule.weekly_rest_minutes=rest.input;scheduleGrid.append(rest.wrap);schedule.append(scheduleGrid);body.append(schedule);
  const calendar=node("details");calendar.append(node("summary","날짜별 휴일·출근 상태"),node("p","계산에 필요한 날짜와 전후 주의 날짜를 직접 확인합니다. 공휴일 여부나 출근 여부를 자동 가정하지 않습니다.","muted"));const calendarRows=node("div");calendar.append(calendarRows,button("날짜 추가",()=>{addCalendarRow(calendarRows,{});changed();}));for(const day of existing.days||[])addCalendarRow(calendarRows,day);body.append(calendar);
  const works=node("details");works.append(node("summary","실제 근무·휴게시간"),node("p","한국 시각으로 입력하세요. 자정을 넘긴 근무는 종료 날짜도 바꿔 주세요. 휴게가 없으면 ‘휴게 위치·없음을 확인함’을 선택합니다.","muted"));const workRows=node("div");works.append(workRows,button("근무기록 추가",()=>{addWorkRow(workRows,{});changed();}));for(const record of current.draft.work_records||[])addWorkRow(workRows,record);body.append(works);workCandidateTable(workRows,current.materials?.work_rows||[]);
  const coverage=node("details");coverage.append(node("summary","근무·휴일을 확인한 자료의 날짜 범위"),node("p","확인한 전체 날짜 범위를 넣습니다. 전후 주의 근무는 조건 확인에 사용되며 이번 지급액에 중복 합산하지 않습니다.","muted"));const ranges=node("div");coverage.append(ranges,button("자료 범위 추가",()=>{addCoverageRow(ranges,{});changed();}));for(const period of current.draft.coverage||[])addCoverageRow(ranges,period);body.append(coverage);
  const included=node("details");included.append(node("summary","이미 기본급 등에 포함된 수당"),node("p","포함액을 확인한 수당만 입력하세요. 포함되지 않음을 확인했다면 0원으로 입력할 수 있습니다.","muted"));
  for(const [id,text] of Object.entries(wageNames)){const previous=(existing.included_allowances||[]).find(item=>item.rule_id===id),row=node("div",undefined,"field-pair"),amount=control("input",text+" 포함액 (원)",previous?.amount,"number"),state=stateChoice(previous?.state);amount.input.min="0";amount.input.step="0.01";row.append(amount.wrap,state.wrap);included.append(row);allowanceControls.push({id,amount:amount.input,state:state.input,previous});}body.append(included);
  const mappings=node("details");mappings.append(node("summary","계산 항목과 급여명세서 항목 연결"),node("p","같은 의미와 포함 범위의 항목만 연결하세요. 하나의 명세서 항목이 여러 계산 항목을 포함하면 함께 선택합니다.","muted"));const mappingRows=node("div");mappings.append(mappingRows,button("항목 연결 추가",()=>{addMappingRow(mappingRows,{});changed();}));for(const value of existing.statement_mappings||[])addMappingRow(mappingRows,value);body.append(mappings);
}
function removeRow(row,list,entry) {row.remove();const index=list.indexOf(entry);if(index>=0)list.splice(index,1);changed();}
function addCalendarRow(target,previous) {
  const row=node("fieldset",undefined,"typed-row"),date=control("input","날짜",previous.date,"date"),kind=choice("날짜 구분",[["","확인 필요"],["workday","근무일"],["unpaid_rest","무급 휴무일"],["paid_holiday","유급 휴일"],["weekly_rest","주휴일"]],previous.kind),attendance=choice("출근 상태",[["","상태 선택"],["present","출근"],["absent","결근"],["paid_leave","유급 휴가"],["not_scheduled","근무 예정 없음"],["unknown","모름"],["pending","확인 예정"],["conflict","자료 간 충돌"]],previous.attendance),confirmed=node("input"),check=node("label",undefined,"checkbox");confirmed.type="checkbox";confirmed.checked=Boolean(previous.confirmed);check.append(confirmed,node("span","날짜 구분을 확인함"));
  const entry={previous,date:date.input,kind:kind.input,attendance:attendance.input,confirmed};calendarControls.push(entry);row.append(node("legend","날짜 확인"),date.wrap,kind.wrap,attendance.wrap,check,button("날짜 삭제",()=>removeRow(row,calendarControls,entry)));target.append(row);
}
function localDateTime(value) {return value?String(value).slice(0,16):"";}
function koreanDateTime(value) {if(!/^\d{4}-\d\d-\d\dT\d\d:\d\d$/.test(value))throw new Error("근무와 휴게의 날짜·시각을 모두 입력해 주세요.");return value+":00+09:00";}
function addWorkRow(target,previous) {
  const row=node("fieldset",undefined,"typed-row"),start=control("input","근무 시작 (한국 시각)",localDateTime(previous.start_at),"datetime-local"),end=control("input","근무 종료 (한국 시각)",localDateTime(previous.end_at),"datetime-local"),breaks=control("textarea","휴게시간 (한 줄에 시작 ~ 종료)",(previous.breaks||[]).map(([a,b])=>localDateTime(a)+" ~ "+localDateTime(b)).join("\n")),known=node("input"),check=node("label",undefined,"checkbox");breaks.input.rows=2;breaks.input.placeholder="2026-10-01T12:00 ~ 2026-10-01T13:00";known.type="checkbox";known.checked=previous.break_location_known===true;check.append(known,node("span","휴게 위치·없음을 확인함"));const entry={previous,start:start.input,end:end.input,breaks:breaks.input,known,id:previous.record_id||"work-manual-"+Date.now()+"-"+(++localRowSequence)};workControls.push(entry);row.append(node("legend","실제 근무"),start.wrap,end.wrap,breaks.wrap,check,button("근무 삭제",()=>removeRow(row,workControls,entry)));target.append(row);
}
function addCoverageRow(target,previous) {const row=node("div",undefined,"typed-row"),start=control("input","확인 범위 시작일",previous.start,"date"),end=control("input","확인 범위 종료일",previous.end,"date"),entry={start:start.input,end:end.input};coverageControls.push(entry);row.append(start.wrap,end.wrap,button("범위 삭제",()=>removeRow(row,coverageControls,entry)));target.append(row);}
function addMappingRow(target,previous) {
  const row=node("fieldset",undefined,"typed-row"),rules=[],items=[];row.append(node("legend","같은 범위의 항목 연결"));
  for(const [id,label] of Object.entries(wageNames)){const check=node("input"),wrap=node("label",undefined,"checkbox");check.type="checkbox";check.checked=(previous.rule_ids||[]).includes(id);wrap.append(check,node("span",label));row.append(wrap);rules.push({id,input:check});}
  for(const item of current.materials?.earnings||[]){const check=node("input"),wrap=node("label",undefined,"checkbox");check.type="checkbox";check.checked=(previous.statement_item_ids||[]).includes(item.item_id);wrap.append(check,node("span",item.label+" · "+resultValue(item.amount,"KRW")));row.append(wrap);items.push({id:item.item_id,input:check});}
  const start=control("input","항목 귀속 시작일",previous.period?.start,"date"),end=control("input","항목 귀속 종료일",previous.period?.end,"date"),state=stateChoice(previous.state),entry={previous,rules,items,start:start.input,end:end.input,state:state.input};mappingControls.push(entry);row.append(start.wrap,end.wrap,state.wrap,button("항목 연결 삭제",()=>removeRow(row,mappingControls,entry)));target.append(row);
}
function requiredPeriod(start,end) {if(!start||!end||start>end)throw new Error("날짜 범위의 시작일과 종료일을 확인해 주세요.");return {start,end};}
function readWageForm() {
  if(wageFormCase!==current?.id||!wageControls.enabled)return {wage_conditions:current?.draft?.wage_conditions??null,work_records:current?.draft?.work_records||[],coverage:current?.draft?.coverage||[]};
  if(!wageControls.enabled.checked)return {wage_conditions:null,work_records:current.draft.work_records||[],coverage:current.draft.coverage||[]};
  const previous=current.draft.wage_conditions||{},value={...previous};
  for(const key of Object.keys(boolConditions)){const selected=wageControls[key].value;value[key]={...(previous[key]||{}),state:["yes","no"].includes(selected)?"known":selected,value:["yes","no"].includes(selected)?selected==="yes":null};}
  for(const key of Object.keys(numberConditions)){const item=wageControls[key],raw=item.input.value.trim();value[key]=conditionValue(item.state,raw===""?null:key==="monthly_standard_minutes"?Number(raw):raw,previous[key]);}
  const schedule=wageControls.schedule;let scheduleValue=null;
  if(schedule.state.value==="known"){
    if(!schedule.week_start.value||!schedule.weekly_rest_weekday.value||schedule.weekly_rest_minutes.value==="")throw new Error("주 일정의 시작 요일·주휴일·주휴시간을 확인해 주세요.");
    scheduleValue={days:schedule.days.flatMap((input,weekday)=>input.value===""?[]:[{weekday,minutes:Number(input.value)}]),week_start:Number(schedule.week_start.value),weekly_rest_weekday:Number(schedule.weekly_rest_weekday.value),weekly_rest_minutes:Number(schedule.weekly_rest_minutes.value)};
  }
  value.schedule=conditionValue(schedule.state,scheduleValue,previous.schedule);
  value.days=calendarControls.map(item=>{if(!item.date.value||!item.kind.value||!item.attendance.value)throw new Error("추가한 날짜의 날짜 구분과 출근 상태를 선택해 주세요.");return {...item.previous,date:item.date.value,kind:item.kind.value,attendance:item.attendance.value,confirmed:item.confirmed.checked};});
  value.included_allowances=allowanceControls.flatMap(item=>{if(!item.amount.value.trim()&&item.state.value==="missing")return [];if(!item.amount.value.trim())throw new Error("포함 수당의 금액을 확인하거나 미입력 상태로 두세요.");return [{...(item.previous||{}),rule_id:item.id,amount:item.amount.value.trim(),state:item.state.value}];});
  value.statement_mappings=mappingControls.map(item=>({...item.previous,rule_ids:item.rules.filter(value=>value.input.checked).map(value=>value.id),statement_item_ids:item.items.filter(value=>value.input.checked).map(value=>value.id),period:requiredPeriod(item.start.value,item.end.value),state:item.state.value}));
  value.calculation_period=$("period-start").value&&$("period-end").value?requiredPeriod($("period-start").value,$("period-end").value):null;
  const work_records=workControls.map(item=>({...item.previous,record_id:item.id,start_at:koreanDateTime(item.start.value),end_at:koreanDateTime(item.end.value),evidence_refs:item.previous.evidence_refs||[],break_location_known:item.known.checked,breaks:item.breaks.value.trim()?item.breaks.value.trim().split(/\n+/).map(line=>{const pieces=line.split("~").map(value=>value.trim());if(pieces.length!==2)throw new Error("휴게시간을 ‘시작 ~ 종료’ 형태로 한 줄씩 입력해 주세요.");return pieces.map(koreanDateTime);}):[]}));
  return {wage_conditions:value,work_records,coverage:coverageControls.map(item=>requiredPeriod(item.start.value,item.end.value))};
}
function renderWageResults() {
  const target=$("wage-results");target.replaceChildren();const view=resultView(),result=view.analysis;if(!result)return;
  const items=result.wage_section?.items||[];if(!items.length)return;
  const enabled=Boolean(view.snapshot?.wage_conditions||!view.historical&&!view.stale&&current?.draft?.wage_conditions);if(!enabled&&!items.some(item=>item.status==="calculated")){target.append(node("p","임금 계산을 선택하지 않은 비교입니다. 연결한 급여명세서와 입금 등 가능한 자료 대조를 확인하세요.","muted"));return;}
  target.append(node("h3","임금 계산 내역"));const summary=result.wage_section||{},scope=summary.scope_complete;
  target.append(node("p",scope?"전체 계산 범위가 확인되었습니다.":"부분 계산입니다. 보류한 항목을 전체 임금에 포함해 추정하지 않습니다.",scope?"scope-complete":"result-stale"));
  const total=summary.gross_total;if(scope&&total!=null)target.append(node("strong","계산 총액 "+resultValue(total,"KRW"),"wage-total"));
  for(const item of items){const card=node("details",undefined,"wage-result"),amount=item.amount,heading=node("summary");const included=Number(item.included_amount||0)>0,caption=item.status==="not_applicable"?"해당 없음":included?(item.operands?.included_in_monthly_base==="true"?"월급에 포함":"이미 포함 "+resultValue(item.included_amount,"KRW"))+" · 추가 "+resultValue(item.additional_amount,"KRW"):amount==null?"확인 필요":resultValue(amount,item.unit||"KRW");heading.append(node("strong",wageNames[item.rule_id]||item.rule_id),node("span",caption));card.append(heading);
    const formula=wageFormula(item);if(formula)card.append(node("p",formula));
    if(item.included_amount!=null)card.append(node("p","이미 포함된 금액: "+resultValue(item.included_amount,"KRW")));if(item.additional_amount!=null)card.append(node("p","추가 지급 계산액: "+resultValue(item.additional_amount,"KRW")));if(item.payable_amount!=null)card.append(node("p","총액에 반영한 금액: "+resultValue(item.payable_amount,"KRW")));
    for(const [key,value] of Object.entries(item.operands||{})){const labels={monthly_contract_amount:"계약 월급 (원)",separated_fixed_allowances:"구분한 고정수당 (원)",base_hourly_rate:"기본 시급 (원)",regular_minutes:"기본근로 (분)",ordinary_hourly_rate:"통상시급 (원)",ordinary_monthly_amount:"월 통상임금 금액 (원)",monthly_standard_minutes:"월 산정 기준시간 (분)",weekly_rest_minutes:"주휴 인정시간 (분)",weekly_scheduled_minutes:"주 소정근로 (분)",overtime_minutes:"연장근로 (분)",night_minutes:"야간근로 (분)",holiday_first_minutes:"휴일근로 8시간 이내 (분)",holiday_extra_minutes:"휴일근로 8시간 초과 (분)",multiplier:"적용 배수",multipliers:"적용 배수"};if(labels[key])card.append(node("p",labels[key]+": "+number(value)));}
    for(const reason of item.reason_codes||[])card.append(node("p",resultReasonNames[reason]||"계산 조건 또는 필요한 날짜의 자료를 확인해 주세요.","muted"));
    for(const id of item.evidence_refs||[]){const evidence=result.evidence_catalog?.find(entry=>entry.evidence_id===id);if(evidence)card.append(resultEvidenceButton(view,evidence));}
    if(item.operands?.included_in_monthly_base==="true")card.append(node("p","월 기본급에 포함된 주휴분입니다. 총액에 다시 더하지 않습니다.","muted"));
    for(const url of item.official_sources||[]){if(!/^https:\/\//.test(url))continue;const link=node("a","계산 규칙의 공식 근거 ↗","glossary-link");link.href=url;link.target="_blank";link.rel="noopener";card.append(link);}target.append(card);
  }
}
function wageFormula(item) {
  const values=item.operands||{},rate=values.ordinary_hourly_rate!=null?number(values.ordinary_hourly_rate)+"원":values.ordinary_monthly_amount!=null?"("+number(values.ordinary_monthly_amount)+"원 × 60 ÷ "+number(values.monthly_standard_minutes)+"분)":"확인한 통상시급";
  if(item.amount==null)return "필요한 조건을 확인한 뒤 계산합니다.";
  if(item.rule_id==="W-01")return values.monthly_contract_amount!=null?number(values.monthly_contract_amount)+"원 − 구분한 고정수당 "+number(values.separated_fixed_allowances)+"원":number(values.base_hourly_rate)+"원 × "+number(values.regular_minutes)+"분 ÷ 60";
  if(item.rule_id==="W-02")return rate+" × "+number(values.weekly_rest_minutes)+"분 ÷ 60";
  if(item.rule_id==="W-03")return rate+" × "+number(values.overtime_minutes)+"분 ÷ 60 × "+values.multiplier;
  if(item.rule_id==="W-04")return rate+" × "+number(values.night_minutes)+"분 ÷ 60 × "+values.multiplier;
  if(item.rule_id==="W-05")return rate+" × (8시간 이내 "+number(values.holiday_first_minutes)+"분 × 1.5 + 초과 "+number(values.holiday_extra_minutes)+"분 × 2) ÷ 60";
  return "";
}
function selectedQuestions() {return resultView().analysis?.questions||[];}
function renderQuestionPage() {
  const target=$("question-list"),view=resultView();target.replaceChildren();
  if(!view.analysis){target.append(node("p","계산·대조를 실행하면 필요한 확인 질문이 표시됩니다."),pageLink("compare","계산·대조로 이동"));return;}
  if(!selectedQuestions().length){target.append(node("p","이 결과에 추가 확인 질문이 없습니다."));return;}
  for(const question of selectedQuestions()){
    const card=node("article",undefined,"question-card");card.append(node("h3",question.text));if(question.reason)card.append(node("p","확인 이유: "+question.reason));
    if(question.required_materials?.length)card.append(node("p","필요한 자료: "+question.required_materials.join(" · ")));
    if(question.required_periods?.length)card.append(node("p","필요한 날짜: "+question.required_periods.map(period=>period.start+" ~ "+period.end).join(" · ")));
    if(question.blocking_rule_ids?.length)card.append(node("p","영향 항목: "+question.blocking_rule_ids.map(id=>wageNames[id]||resultRules[id]?.title||id).join(" · ")));
    const fields=(question.target_field_ids||[]).map(field=>field.replace(/^wage_conditions\./,"")),answer=(Array.isArray(current.question_answers)?current.question_answers.flatMap(entry=>entry.answers||[]):[]).findLast(entry=>entry.question_id===question.question_id);if(answer)card.append(node("p","답변 상태: "+(stateNames[answer.state]||"저장됨")));
    for(const id of question.glossary_ids||[])card.append(glossaryLink(id,"관련 용어 설명"));
    const scalar=fields.find(field=>Object.hasOwn(boolConditions,field)||Object.hasOwn(numberConditions,field));
    if(scalar&&!view.historical&&!view.stale){const previous=current.draft?.wage_conditions?.[scalar],state=Object.hasOwn(boolConditions,scalar)?boolChoice(boolConditions[scalar],previous):stateChoice(previous?.state),value=Object.hasOwn(numberConditions,scalar)?control("input",numberConditions[scalar],previous?.value,"number"):null;card.append(state.wrap);if(value)card.append(value.wrap);
      card.append(button("이 답변을 확인 입력에 반영",async()=>{let selected=state.input.value,typed=value?.input.value.trim()||null;if(Object.hasOwn(boolConditions,scalar)){typed=["yes","no"].includes(selected)?selected==="yes":null;selected=["yes","no"].includes(selected)?"known":selected;}else if(scalar==="monthly_standard_minutes"&&typed!==null)typed=Number(typed);if(selected!=="known")typed=null;await flushDraft();current=await api(caseApiPath()+"/answers","PUT",{result_id:view.analysis.result_id,answers:[{question_id:question.question_id,field_id:scalar,state:selected,value:typed}],confirm:true});syncActiveRecord();renderCase();notice("답변을 확인 입력에 반영했습니다. 재계산하면 새 결과가 생성됩니다.");}));
    }
    card.append(button("관련 입력 보완하기",async()=>{await navigateRoute("compare");const key=scalar||fields[0],input=$("condition-"+key);if(input&&typeof input.focus==="function"){for(let parent=input.parentElement;parent;parent=parent.parentElement)if(parent.tagName==="DETAILS")parent.open=true;input.focus();}else if(typeof $("wage-conditions").scrollIntoView==="function")$("wage-conditions").scrollIntoView({block:"start"});}));target.append(card);
  }
  target.append(button("확인한 입력으로 다시 계산",async()=>{await start("analyze");},"primary"));
}
function pageResultKey() {return (current?.id||"")+":"+(resultView().analysis?.result_id||"");}
function savedInquiry(record,resultId) {return record?.inquiries?.[resultId]||record?.analysis_history?.find(entry=>entry.analysis?.result_id===resultId)?.inquiry||(record?.inquiry?.result_id===resultId?record.inquiry:null)||record?.inquiry_history?.findLast(entry=>entry.result_id===resultId)||null;}
function renderInquiryPage() {
  const view=resultView(),key=pageResultKey();if(inquiryKey!==key){inquiryKey=key;inquiryUnsaved=false;inquiryRecord=savedInquiry(current,view.analysis?.result_id);$("inquiry-text").value=inquiryRecord?.text||"";}
  $("inquiry-version-note").replaceChildren(node("p",inquiryRecord?.edited?"사용자가 수정한 초안입니다. 새로 생성해도 저장된 수정본을 자동 덮어쓰지 않습니다.":"차이를 미지급 확정으로 표현하지 않는 확인용 초안입니다.","muted"));
  if(questionSelectionKey!==key){questionSelectionKey=key;inquiryQuestionInputs=new Map();const target=$("inquiry-question-options");target.replaceChildren();for(const question of selectedQuestions()){const label=node("label",undefined,"checkbox"),input=node("input");input.type="checkbox";input.checked=(inquiryRecord?.selected_question_ids||[]).includes(question.question_id);label.append(input,node("span",question.text));target.append(label);inquiryQuestionInputs.set(question.question_id,input);}}
  $("inquiry-save-state").textContent=inquiryUnsaved?"수정 중 · 저장 필요":inquiryRecord?"저장한 초안":"초안 생성 전";
}
async function flushInquiry() {
  clearTimeout(inquiryTimer);if(!inquiryUnsaved||!inquiryKey)return inquirySaving;
  const record=current,view=resultView(),key=inquiryKey,text=$("inquiry-text").value;if(!record||!view.analysis)return;
  inquirySaving=inquirySaving.catch(()=>{}).then(async()=>{const saved=await api(caseApiPath(record)+"/inquiry","PUT",{result_id:view.analysis.result_id,text});if(inquiryKey===key){if($("inquiry-text").value===text)inquiryUnsaved=false;inquiryRecord=savedInquiry(saved,view.analysis.result_id)||saved;if(current?.id===record.id&&saved.id){current=saved;syncActiveRecord();}$("inquiry-save-state").textContent=inquiryUnsaved?"추가 수정 있음 · 저장 필요":"수정 내용 저장됨";}});await inquirySaving;
}
async function generateInquiry() {await flushInquiry();const view=resultView();if(!view.analysis)return;const saved=await api(caseApiPath()+"/inquiry","POST",{result_id:view.analysis.result_id,question_ids:[...inquiryQuestionInputs].filter(([,input])=>input.checked).map(([id])=>id)});current=saved;syncActiveRecord();inquiryKey="";renderInquiryPage();notice("문의 초안을 준비했습니다. 기존 수정본이 있으면 보존합니다.");}
function reportSources(view) {if(view.report_sources?.length)return view.report_sources;if(!view.historical&&current?.report_sources?.length)return current.report_sources;return (view.snapshot?.authorized_sources||[]).filter(source=>source.kind==="document").map(source=>({source_id:source.source_id,document_id:source.document_id,filename:resultSources(view).find(item=>item.ref?.source_id===source.source_id)?.title||docs.find(item=>item.id===source.document_id)?.title||"원본 PDF"}));}
function renderReportPage() {
  const view=resultView(),target=$("report-preview"),key=pageResultKey();target.replaceChildren();
  if(!view.analysis){target.append(node("p","먼저 계산·대조 결과를 만들어 주세요."),pageLink("compare","계산·대조로 이동"));return;}
  target.append(node("p","보고서 기준: "+resultPeriod(view)+" · 확인 입력 v"+view.analysis.input_version),node("p","사업장과 기간, 확인 조건, 계산 내역과 범위, 문서 대조표, 확인 질문, 자료·출처가 포함됩니다.","muted"),pageLink("results","같은 버전의 수치와 근거 보기"));
  if(view.stale||view.historical)target.append(node("p","현재 입력과 다른 과거 결과임을 보고서에서도 구분합니다.","result-stale"));
  if(reportSelectionKey!==key){reportSelectionKey=key;reportSourceInputs=new Map();const options=$("report-source-options");options.replaceChildren();let sources=reportSources(view);if(!sources.length&&!view.historical)sources=current.report_sources||[];for(const source of sources){const label=node("label",undefined,"checkbox"),input=node("input");input.type="checkbox";label.append(input,node("span",source.filename||source.title||"원본 PDF"));options.append(label);reportSourceInputs.set(source.source_id,input);}if(!sources.length)options.append(node("p","이 결과에 연결된 원본이 없습니다.","muted"));}
  const downloads=$("report-downloads");downloads.replaceChildren();const all=[current.report,...(current.report_history||[])].filter(Boolean);for(const report of all.filter(report=>report.analysis_result_id===view.analysis.result_id||report.result_id===view.analysis.result_id||report.source_result_id===view.analysis.result_id||report===current.report&&!view.historical&&!view.stale)){for(const artifact of report.artifacts||[]){const link=node("a",artifact.mime_type==="application/pdf"?"보고서 PDF 받기 ↓":"자료 묶음 ZIP 받기 ↓","download-link");link.href="/api/artifacts/"+artifact.artifact_id;link.download="";downloads.append(link);}}
  $("report-job-state").textContent=currentJob?.stage==="report"?busy()?"보고서 생성 중입니다.":currentJob.status==="completed"?"보고서 생성 완료":"보고서 상태는 개발 정보에서 확인해 주세요.":"";
}
async function startReport() {await flushDraft();const view=resultView();if(!view.analysis)throw new Error("보고서로 만들 결과를 선택해 주세요.");currentJob=await api(caseApiPath()+"/jobs","POST",{stage:"report",result_id:view.analysis.result_id,selected_source_ids:[...reportSourceInputs].filter(([,input])=>input.checked).map(([id])=>id)});renderJob();pollJob();}
function updatePageControls() {const view=resultView(),running=Boolean(busy()||loadingMaterials||comparisonSubmitting),available=Boolean(view.analysis);$("report").disabled=!available||running;$("inquiry-generate").disabled=!available||running;$("inquiry-save").disabled=!available||running;$("inquiry-copy").disabled=!$("inquiry-text").value;}
function glossaryLink(id,label) {const link=node("a",label,"glossary-link"),back=routeUrl(activeRoute);link.href="/glossary?term="+encodeURIComponent(id)+"&return="+encodeURIComponent(back);link.addEventListener("click",safeAction(async event=>{event.preventDefault();await flushDraft();await flushInquiry();const anchor=link.closest("[id]")?.id;const returnUrl=back+(anchor?"#"+encodeURIComponent(anchor):"");link.href="/glossary?term="+encodeURIComponent(id)+"&return="+encodeURIComponent(returnUrl);history.pushState(null,"",link.href);activeRoute="glossary";renderRoute();await loadGlossary();}));return link;}
async function loadGlossary() {if(glossaryLoading)return glossaryLoading;if(glossaryLoaded){renderGlossary();return;}glossaryLoading=(async()=>{const data=await api("/api/glossary");glossaryEntries=data.entries||[];glossaryLoaded=true;const select=$("glossary-category");select.replaceChildren();for(const category of ["",...new Set(glossaryEntries.map(item=>item.category).filter(Boolean))]){const option=node("option",category||"모든 주제");option.value=category;select.append(option);}renderGlossary();})();try{await glossaryLoading;}finally{glossaryLoading=null;}}
function renderGlossary() {const target=$("glossary-entries"),params=new URLSearchParams(location.search),selected=params.get("term"),back=params.get("return"),query=$("glossary-search").value.trim().toLocaleLowerCase(),category=$("glossary-category").value;target.replaceChildren();$("glossary-return").href=back&&/^\/(ocr|ai|compare|results|questions|inquiry|reports)(\?|$)/.test(back)?back:routeUrl("compare");if(!glossaryLoaded){target.append(node("p","용어를 불러오는 중입니다."));return;}const entries=glossaryEntries.filter(item=>(!category||item.category===category)&&(!query||[item.term,item.explanation,item.distinction].join(" ").toLocaleLowerCase().includes(query)));for(const entry of entries){const card=node("article",undefined,"glossary-card"+(entry.id===selected?" selected":""));card.id="term-"+entry.id;card.setAttribute("tabindex","-1");card.append(node("span",entry.category||"급여 용어","tag"),node("h3",entry.term),node("p",entry.explanation),node("p",entry.example,"glossary-example"));if(entry.distinction)card.append(node("p",entry.distinction,"muted"));if(entry.sources?.length){const details=node("details");details.append(node("summary","설명 근거"));for(const source of entry.sources){if(!/^https:\/\//.test(source.url||""))continue;const link=node("a",source.title||"공식 자료");link.href=source.url;link.target="_blank";link.rel="noopener";details.append(link);}card.append(details);}target.append(card);}if(!entries.length)target.append(node("p","일치하는 용어가 없습니다."));if(selected){const card=$("term-"+selected);if(card&&typeof card.scrollIntoView==="function")card.scrollIntoView({block:"center"});}}
function initializeWorkPages() {
  $("inquiry-generate").addEventListener("click",safeAction(generateInquiry));$("inquiry-save").addEventListener("click",safeAction(flushInquiry));
  $("inquiry-text").addEventListener("input",()=>{inquiryUnsaved=true;$("inquiry-save-state").textContent="수정 중 · 저장 필요";updatePageControls();clearTimeout(inquiryTimer);inquiryTimer=setTimeout(()=>flushInquiry().catch(error=>notice("문의 초안을 저장하지 못했습니다. "+error.message,true)),600);});
  $("inquiry-copy").addEventListener("click",safeAction(async()=>{await flushInquiry();if(typeof navigator!=="undefined"&&navigator.clipboard?.writeText){await navigator.clipboard.writeText($("inquiry-text").value);notice("문의 내용을 복사했습니다.");}else{$("inquiry-text").focus();$("inquiry-text").select();notice("문의 내용을 선택했습니다. 복사 단축키를 눌러 주세요.");}}));
  $("report-select-none").addEventListener("click",()=>{for(const input of reportSourceInputs.values())input.checked=false;});$("report-select-all").addEventListener("click",()=>{for(const input of reportSourceInputs.values())input.checked=true;});
  $("glossary-search").addEventListener("input",renderGlossary);$("glossary-category").addEventListener("change",renderGlossary);
  window.addEventListener("beforeunload",event=>{if(pendingDraft||inquiryUnsaved){event.preventDefault();event.returnValue="";}});
}
