const $ = (selector) => document.querySelector(selector);
const $$ = (selector) => [...document.querySelectorAll(selector)];
const state = { conversationId: null, analysis: null, context: null, deployments: [], busy: new Set() };
const titles = {
  chat: ["인프라 채팅", "인프라를 이해하는 대화", "설정과 근거를 바탕으로 인프라를 확인하세요."],
  analysis: ["OTel 분석", "운영 지표에서 찾는 단서", "관측된 수치와 데이터 부족을 함께 확인하세요."],
  recommendation: ["인프라 구성", "운영 지표로 다음 인프라 구성", "현재 앱 배포와 대상 환경을 선택하면 클라우드 운영 지표로 IaC 구성을 추천합니다."],
};
const names = {
  compute_instance_type: "컴퓨트 인스턴스", compute_machine_type: "컴퓨트 머신",
  compute_disk_gib: "컴퓨트 디스크 (GiB)", db_instance_class: "DB 인스턴스", db_tier: "DB 사양",
  db_storage_gib: "DB 저장 공간 (GiB)", db_max_storage_gib: "DB 최대 저장 공간 (GiB)",
  db_multi_az: "DB 다중 가용 영역", db_high_availability: "DB 고가용성",
  cache_node_type: "Redis 노드 사양", cache_node_count: "Redis 노드 수",
  cache_memory_gib: "Redis 메모리 (GiB)", cache_high_availability: "Redis 고가용성",
  cpu: "CPU (vCPU)", memory_mib: "메모리 (MiB)",
};
const metricLabels = {
  cpuP95Percent: ["CPU P95", "%"], memoryPeakMiB: ["메모리 피크", "MiB"],
  requestP95Ms: ["응답 시간 P95", "ms"], requestRatePerSecond: ["초당 요청 수", "req/s"],
  errorRatePercent: ["서버 오류율", "%"], databaseP95Ms: ["DB 지연 P95", "ms"],
  redisP95Ms: ["Redis 지연 P95", "ms"],
};
const sourceTypes = {
  CONFIGURATION: "설정 정보", SAVED_BINDING: "저장된 연결 정보", WORKFLOW: "전환 작업 기록",
  USER_APPLICATION_DEPLOYMENT: "내 앱 배포의 적용 기록", USER_APPLICATION_INVENTORY: "내 앱 배포 목록",
};
const statusLabels = { COMPLETED: "분석 완료", PARTIAL: "일부 지표만 확인됨", INSUFFICIENT_DATA: "분석할 지표 없음" };
const categoryLabels = { RESOURCE: "자원", PERFORMANCE: "성능", ERROR: "오류", DATA_QUALITY: "데이터 품질" };
const severityLabels = { INFO: "정보", WARNING: "주의", CRITICAL: "확인 필요" };

function el(tag, className = "", text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = String(text);
  return node;
}

function showError(message = "") {
  $("#global-error").textContent = message;
  $("#global-error").hidden = !message;
}

function setView(view) {
  for (const button of $$("[data-view]")) {
    const active = button.dataset.view === view;
    button.classList.toggle("active", active);
    if (active) button.setAttribute("aria-current", "page");
    else button.removeAttribute("aria-current");
  }
  for (const name of Object.keys(titles)) $(`#${name}-view`).hidden = name !== view;
  const [label, title, description] = titles[view];
  $("#breadcrumb").textContent = label;
  $("#page-title").textContent = title;
  $("#page-description").textContent = description;
  showError();
}

function busy(name, value) {
  if (value) state.busy.add(name);
  else state.busy.delete(name);
  const button = $(`#${name}-submit`);
  button.disabled = value || (name !== "chat" && !state.deployments.some((d) => ["READY", "INFRA_READY", "APP_NOT_READY"].includes(d.status)));
  button.setAttribute("aria-busy", String(value));
  if (name === "chat") {
    $("#new-chat").disabled = value;
    $("#chat-message").disabled = value;
    $("#chat-environment").disabled = value || Boolean(state.conversationId);
    for (const suggestion of $$("[data-prompt]")) suggestion.disabled = value;
  }
}

async function api(path, body) {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 150000);
  try {
    const response = await fetch(path, {
      method: body === undefined ? "GET" : "POST",
      headers: body === undefined ? {} : { "Content-Type": "application/json", "Idempotency-Key": crypto.randomUUID() },
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: controller.signal,
    });
    const data = await response.json();
    if (!response.ok) {
      const messages = {
        AI_NOT_CONFIGURED: "서버의 .env에 OpenAI 키를 설정한 뒤 서버를 다시 실행해 주세요.",
        AI_TIMEOUT: "모델 응답 시간이 초과됐습니다. 잠시 후 다시 실행해 주세요.",
        AI_UNAVAILABLE: "모델에 연결할 수 없습니다. 서버의 키와 모델 설정을 확인해 주세요.",
        AI_INVALID_RESPONSE: "답변이 근거 또는 사양 검사를 통과하지 못했습니다. 다시 요청해 주세요.",
        CONVERSATION_SCOPE_MISMATCH: "환경을 바꾸려면 새 대화를 시작해 주세요.",
        NOT_FOUND: "기록을 찾을 수 없습니다. 서버가 재시작됐다면 새 대화를 시작해 주세요.",
      };
      throw new Error(messages[data.code] || data.message || `요청을 처리하지 못했습니다. (${response.status})`);
    }
    return data;
  } catch (error) {
    if (error.name === "AbortError") throw new Error("응답을 기다리는 시간이 초과됐습니다. 서버 상태를 확인해 주세요.");
    if (error instanceof TypeError) throw new Error("로컬 서버에 연결할 수 없습니다. 서버가 실행 중인지 확인해 주세요.");
    throw error;
  } finally { clearTimeout(timeout); }
}

function formatTime(time) {
  return time ? new Intl.DateTimeFormat("ko-KR", { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit", timeZone: "Asia/Seoul" }).format(new Date(time)) : "관측 시점 없음";
}

function rawResponse(data) {
  const details = el("details", "raw-response");
  details.append(el("summary", "", "원본 응답 보기"), el("pre", "", JSON.stringify(data, null, 2)));
  return details;
}

function addMessage(role, text, pending = false) {
  $("#chat-welcome").hidden = true;
  const message = el("article", `message ${role}`);
  const body = el("div", "message-body");
  const content = el("div", pending ? "pending-text" : "message-text", text);
  body.append(el("p", "message-role", role === "user" ? "나" : "FOXTROT AI"), content);
  message.append(el("div", "message-avatar", role === "user" ? "나" : "✧"), body);
  $("#chat-feed").append(message);
  $("#chat-feed").scrollTop = $("#chat-feed").scrollHeight;
  return { message, body, content };
}

async function sendChat() {
  const input = $("#chat-message");
  const message = input.value.trim();
  if (!message || state.busy.has("chat")) return;
  const environment = $("#chat-environment").value;
  showError();
  addMessage("user", message);
  input.value = "";
  busy("chat", true);
  const pending = addMessage("assistant", "설정과 근거를 확인하고 있습니다", true);
  try {
    const result = await api("/v1/ai/chat", {
      applicationId: state.context?.applicationId,
      environments: environment ? [environment] : [],
      conversationId: state.conversationId,
      message,
    });
    state.conversationId = result.conversationId;
    pending.content.className = "message-text";
    pending.content.textContent = result.answer;
    if (result.sources?.length) {
      const sources = el("details", "sources");
      sources.append(el("summary", "", `근거 보기 · ${result.sources.length}개`));
      for (const source of result.sources) {
        const item = el("div", "source-item");
        item.append(el("strong", "", sourceTypes[source.type] || source.type), el("p", "", `${source.id} · ${formatTime(source.observedAt)}`));
        sources.append(item);
      }
      pending.body.append(sources);
    }
    if (result.warnings?.length) pending.body.append(el("p", "message-warning", result.warnings.join("\n")));
  } catch (error) {
    pending.content.className = "message-text";
    pending.content.textContent = error.message;
    showError(error.message);
  } finally {
    busy("chat", false);
    input.focus();
    $("#chat-feed").scrollTop = $("#chat-feed").scrollHeight;
  }
}

function loading(target, message) {
  const card = el("div", "card empty-result");
  card.append(el("div", "pending-text", message), el("p", "form-note", "처리가 끝나면 이곳에 결과를 표시합니다."));
  $(target).replaceChildren(card);
}

function renderAnalysis(data) {
  const card = el("article", "card result-card");
  const heading = el("div", "result-heading");
  heading.append(el("h2", "", "운영 지표 분석"), el("span", `result-badge ${data.status === "COMPLETED" ? "" : "warning"}`, statusLabels[data.status] || data.status));
  card.append(heading, el("p", "result-summary", data.summary));
  const grid = el("div", "metrics-grid");
  for (const [key, [label, unit]] of Object.entries(metricLabels)) {
    if (!(key in (data.dataQuality.metrics || {}))) continue;
    const metric = el("div", "metric");
    const value = data.metrics[key];
    const amount = el("p", "metric-value", value == null ? "—" : new Intl.NumberFormat("ko-KR", { maximumFractionDigits: 1 }).format(value));
    if (value != null) amount.append(el("small", "", unit));
    metric.append(el("p", "metric-label", label), amount);
    grid.append(metric);
  }
  card.append(grid);
  const total = Object.keys(data.dataQuality.metrics || {}).length;
  card.append(el("p", "quality-line", `수집된 지표 ${total - data.dataQuality.missingMetrics.length} / ${total} · ${formatTime(data.timeRange.start)} ~ ${formatTime(data.timeRange.end)}`));
  if (data.dataQuality.missingMetrics.length) card.append(el("p", "result-warning", "미수집 지표: " + data.dataQuality.missingMetrics.map((key) => metricLabels[key]?.[0] || key).join(", ") + "\n누락된 값은 0으로 처리하지 않습니다."));
  if (data.findings.length) {
    const findings = el("div", "");
    for (const finding of data.findings) {
      const item = el("div", "finding");
      item.append(el("div", "finding-heading", `${categoryLabels[finding.category] || finding.category} · ${severityLabels[finding.severity] || finding.severity}`), el("p", "", finding.description), el("code", "", finding.evidenceKeys.join(" · ")));
      findings.append(item);
    }
    card.append(findings);
  }
  const next = el("button", "result-action", "이 분석으로 사양 추천하기 →");
  next.type = "button";
  next.addEventListener("click", () => { $("#source-deployment").value = data.sourceDeploymentId; setView("recommendation"); });
  card.append(next, rawResponse(data));
  $("#analysis-result").replaceChildren(card);
}

function parameterTable(parameters, baseline, rationale) {
  const table = el("table", "params-table");
  const head = el("thead");
  const row = el("tr");
  for (const label of ["항목", "검증 기준", "추천값"]) row.append(el("th", "", label));
  head.append(row); table.append(head);
  const body = el("tbody");
  for (const [key, value] of Object.entries(parameters)) {
    const row = el("tr");
    const label = el("td", "", names[key] || key);
    label.append(el("span", "parameter-key", key));
    const display = (v) => v === undefined || v === null ? "미확인" : typeof v === "boolean" ? (v ? "사용" : "사용 안 함") : String(v);
    row.append(label, el("td", "", display(baseline?.[key])), el("td", "", display(value)));
    body.append(row);
    if (rationale[key]) {
      const reasonRow = el("tr");
      const reason = el("td", "param-rationale", rationale[key]);
      reason.colSpan = 3; reasonRow.append(reason); body.append(reasonRow);
    }
  }
  table.append(body);
  return table;
}

function renderRecommendation(data) {
  const card = el("article", "card result-card");
  if (["INSUFFICIENT_DATA", "CONSTRAINT_LIMIT"].includes(data.status)) {
    card.append(el("h2", "", data.status === "CONSTRAINT_LIMIT" ? "현재 정책에서 추천할 사양이 없습니다" : "추천에 필요한 지표가 부족합니다"),
      el("p", "result-warning", data.warnings.join("\n") + "\n" + data.missingRequiredMetrics.map((key) => metricLabels[key]?.[0] || key).join(", ")),
      el("p", "form-note", "플랫폼 관리 지표나 다른 앱의 지표로 대체하지 않습니다."), rawResponse(data));
    $("#recommendation-result").replaceChildren(card);
    return;
  }
  const heading = el("div", "result-heading");
  heading.append(el("h2", "", `${data.proposal.cloud.toUpperCase()} 사양 추천`), el("span", "result-badge", "내 앱 CPU·메모리 기준"));
  card.append(heading, el("p", "result-summary", "선택한 앱의 적용 사양과 최근 1시간 지표를 대상 환경의 사양 정책에 맞춰 검토했습니다."));
  const changed = Object.keys(data.proposal.parameters);
  card.append(el("h3", "allocation-title", "인프라 사양"));
  if (changed.length) card.append(parameterTable(data.proposal.parameters, data.validationBaseline, data.proposal.rationale));
  else card.append(el("p", "form-note", "인프라 사양의 변경 추천이 없습니다."));
  card.append(el("h3", "allocation-title", "앱 CPU·메모리"));
  if (Object.keys(data.appProposal.parameters).length) card.append(parameterTable(data.appProposal.parameters, null, data.appProposal.rationale));
  else card.append(el("p", "form-note", "앱 할당량의 변경 추천이 없습니다."));
  const allocation = data.effectiveAppAllocation;
  const unit = data.allocation.scope === "TASK" ? "ECS 태스크 전체 공유" : "API·워커 컨테이너당";
  card.append(el("p", "quality-line", `추천 할당량: ${allocation.cpu} vCPU · ${allocation.memory_mib} MiB (${unit})`));
  const totalLabel = data.allocation.tunnelBasis === "RESERVED_IF_CONFIGURED" ? "검증용 전체 할당량 · 터널 여유 포함" : data.allocation.scope === "TASK" ? "태스크 전체" : "인스턴스 전체";
  card.append(el("p", "quality-line", `${totalLabel}: ${data.allocation.totalCpuCores} vCPU · ${data.allocation.totalMemoryMiB} MiB`));
  if (data.iac) {
    const iac = el("details", "raw-response");
    iac.append(el("summary", "", "IaC 반영 요청 보기 · " + (data.iac.action === "UPDATE" ? "기존 배포 수정" : "대상 환경 배포")), el("pre", "", "POST " + data.iac.endpoint + "\n\n" + JSON.stringify(data.iac.body, null, 2)));
    card.append(iac);
  }
  if (data.warnings.length) card.append(el("p", "result-warning", data.warnings.join("\n\n")));
  card.append(el("p", "result-meta", `생성 ${formatTime(data.createdAt)} · 자동 배포되지 않음`), rawResponse(data));
  $("#recommendation-result").replaceChildren(card);
}

async function analyse(event) {
  event.preventDefault();
  if (state.busy.has("analysis")) return;
  const focus = $$("input[name=focus]:checked").map((input) => input.value);
  if (!focus.length) { showError("분석할 항목을 하나 이상 선택해 주세요."); return; }
  showError(); busy("analysis", true); loading("#analysis-result", "운영 지표를 조회하고 있습니다");
  const end = new Date(Date.now() - 10000);
  const start = new Date(end.getTime() - Number($("#analysis-period").value) * 1000);
  try {
    const result = await api("/v1/ai/analyses", { applicationId: state.context?.applicationId, deploymentId: $("#analysis-environment").value, timeRange: { start: start.toISOString(), end: end.toISOString() }, focus });
    state.analysis = result;
    renderAnalysis(result);
  } catch (error) {
    showError(error.message);
    $("#analysis-result").replaceChildren(el("div", "card result-card", error.message));
  } finally { busy("analysis", false); }
}

async function recommend(event) {
  event.preventDefault();
  if (state.busy.has("recommendation")) return;
  showError(); busy("recommendation", true); loading("#recommendation-result", "사양을 추천하고 검증하고 있습니다");
  try {
    const result = await api("/v1/ai/recommendations", {
      applicationId: state.context?.applicationId, targetEnvironment: $("#target-environment").value,
      sourceDeploymentId: $("#source-deployment").value,
    });
    if (result.analysis) { state.analysis = result.analysis; renderAnalysis(result.analysis); }
    renderRecommendation(result);
  } catch (error) {
    showError(error.message);
    $("#recommendation-result").replaceChildren(el("div", "card result-card", error.message));
  } finally { busy("recommendation", false); }
}

function options(selector, values) {
  for (const value of values) {
    const option = el("option", "", value.toUpperCase()); option.value = value; $(selector).append(option);
  }
}

async function boot() {
  try {
    const [status, context, inventory] = await Promise.all([api("/v1/ai/status"), api("/playground/context"), api("/v1/ai/deployments")]);
    state.deployments = inventory.deployments;
    state.context = context;
    $("#application-name").textContent = state.deployments.length ? context.applicationId : "배포된 앱 없음";
    $("#environment-list").textContent = context.environments.map((value) => value.toUpperCase()).join(" · ") || "—";
    $("#configuration-source").textContent = "내가 배포한 앱";
    $("#model-name").textContent = status.model;
    $("#server-status").textContent = "로컬 서버 연결됨";
    for (const [selector, connected] of [["#openai-state", status.configured], ["#metrics-state", status.prometheusConfigured], ["#traces-state", status.tempoConfigured]]) {
      $(selector).textContent = connected ? "연결 설정됨" : "미연결";
      $(selector).classList.toggle("connected", connected);
    }
    options("#chat-environment", context.environments);
    const available = state.deployments.filter((deployment) => ["READY", "INFRA_READY", "APP_NOT_READY"].includes(deployment.status));
    for (const selector of ["#analysis-environment", "#source-deployment"]) {
      if (!available.length) {
        const option = el("option", "", "배포된 앱 없음"); option.value = ""; $(selector).append(option);
      }
      for (const deployment of available) {
        const option = el("option", "", `${deployment.applicationId} · ${deployment.environment.toUpperCase()} · ${deployment.environmentId}`);
        option.value = deployment.environmentId; $(selector).append(option);
      }
    }
    options("#target-environment", status.sizingEnvironments);
    busy("analysis", false); busy("recommendation", false);
    $("#local-notice").textContent = `로컬 테스트 · 내 앱 배포 ${state.deployments.length}개 · 플랫폼 관리 데이터 제외. CPU·메모리는 CloudWatch 또는 Cloud Monitoring에서 조회합니다. 클라우드 권한과 실제 지표는 분석 실행 시 확인합니다.`;
    if (!available.length) $("#recommendation-result").replaceChildren(el("div", "card empty-result", "현재 로그인 사용자에게 연결된 앱 배포가 없습니다. 앱 배포가 연결되면 클라우드 CPU·메모리 기반 추천을 사용할 수 있습니다."));
    if (!status.configured) showError("OpenAI가 설정되지 않았습니다. 서버의 .env에서 키를 설정한 뒤 다시 실행해 주세요.");
  } catch (error) {
    $("#server-status").textContent = "서버 연결 확인 필요";
    $("#server-dot").classList.add("offline");
    showError(error.message);
  }
}

for (const button of $$("[data-view]")) button.addEventListener("click", () => setView(button.dataset.view));
for (const button of $$("[data-prompt]")) button.addEventListener("click", () => { $("#chat-message").value = button.dataset.prompt; sendChat(); });
$("#chat-form").addEventListener("submit", (event) => { event.preventDefault(); sendChat(); });
$("#chat-message").addEventListener("keydown", (event) => { if (event.key === "Enter" && !event.shiftKey && !event.isComposing) { event.preventDefault(); sendChat(); } });
$("#new-chat").addEventListener("click", () => {
  state.conversationId = null;
  for (const message of $$("#chat-feed .message")) message.remove();
  $("#chat-welcome").hidden = false; $("#chat-environment").disabled = false; showError(); $("#chat-message").focus();
});
$("#analysis-form").addEventListener("submit", analyse);
$("#recommendation-form").addEventListener("submit", recommend);
boot();
