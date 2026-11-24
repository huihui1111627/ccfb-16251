'use strict';

let state = null;
let chartOptions = { metric: 'predicted_total_ms', log: true };
let chartModel = null;

const STRATEGY_META = {
  adaptive: { name: '参数敏感分档缓存', color: '#38d39f' },
  single: { name: '只缓存一个计划', color: '#ff6b6b' },
  reopt: { name: '每次重新优化', color: '#5b8cff' },
};

const SIGNATURE_NAMES = {
  index_nl: '索引扫描 + 嵌套循环',
  index_hash: '索引扫描 + Hash Join',
  seq_hash: '全表扫描 + Hash Join',
  seq_nl: '全表扫描 + 嵌套循环',
  index_agg: '索引扫描 + 聚合',
  seq_agg: '全表扫描 + 聚合',
  pk_point: '主键点查',
  seq_scan: '全表扫描',
};

const FAMILY_NAMES = {
  merchant_orders_join: '商户订单 + 商户表连接 / 排序 LIMIT',
  merchant_orders_aggregate: '商户状态聚合',
  order_primary_key: '订单主键点查',
  generic: '通用查询',
};

async function fetchState() {
  const response = await fetch('/api/state', { cache: 'no-store' });
  if (!response.ok) throw new Error(`读取状态失败: ${response.status}`);
  return response.json();
}

async function postJSON(url, body) {
  const response = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  });
  const data = await response.json();
  if (!response.ok || data.error) throw new Error(data.error || `请求失败 ${response.status}`);
  return data;
}

function fmt(value, digits = 2) {
  if (value === null || value === undefined || Number.isNaN(value)) return '-';
  if (typeof value !== 'number') return value;
  if (Math.abs(value) >= 1000) return value.toLocaleString('zh-CN', { maximumFractionDigits: 0 });
  if (Math.abs(value) < 0.01 && value !== 0) return value.toFixed(4);
  return value.toFixed(digits);
}

function pct(value, digits = 3) {
  if (value === 0) return '0%';
  if (value < 0.0001) return `${(value * 100).toExponential(1)}%`;
  return `${(value * 100).toFixed(digits)}%`;
}

function selectivityText(s) {
  if (s === null || s === undefined) return '-';
  if (s < 0.0001) return s.toExponential(2);
  return `${(s * 100).toFixed(3)}%`;
}

function badge(text, cls = 'gray') {
  return `<span class="badge ${cls}">${text}</span>`;
}

function statusBadge(status) {
  const map = {
    HIT: ['HIT 缓存命中', 'green'],
    PINNED_HIT: ['强制计划命中', 'purple'],
    SINGLE_HIT: ['单计划复用', 'orange'],
    MISS: ['MISS', 'red'],
    TIER_REBUILD: ['档位重建', 'orange'],
    PINNED_MISS: ['强制计划生成', 'purple'],
    SINGLE_MISS: ['首个参数定计划', 'red'],
    REOPTIMIZE: ['重新优化', 'blue'],
    BYPASS: ['禁止缓存', 'red'],
  };
  const [text, cls] = map[status] || [status, 'gray'];
  return badge(text, cls);
}

function planBadge(sig) {
  const cls = sig.startsWith('index') || sig === 'pk_point' ? 'green'
    : sig.startsWith('seq') ? 'orange' : 'blue';
  return badge(SIGNATURE_NAMES[sig] || sig, cls);
}

async function refresh(renderAgain = true) {
  try {
    const payload = await fetchState();
    if (payload.status === 'running' || payload.status === 'error') {
      state = null;
      const app = document.getElementById('app');
      const message = payload.status === 'error'
        ? `基准启动失败：${escapeHtml(payload.error || '未知错误')}`
        : '正在生成幂律数据并运行三策略对照基准，首次加载约需十几秒，页面会自动刷新...';
      app.innerHTML = `<section class="loading">${message}</section>`;
      if (renderAgain) setTimeout(() => refresh(true), 1200);
      return;
    }
    state = payload;
    if (renderAgain) render();
  } catch (error) {
    document.getElementById('app').innerHTML =
      `<div class="error-banner">${error.message}<br>服务可能仍在启动，请稍后点击“刷新页面数据”。</div>`;
  }
}

function render() {
  const app = document.getElementById('app');
  const template = document.getElementById('dashboardTemplate');
  app.innerHTML = '';
  app.appendChild(template.content.cloneNode(true));

  renderKpis();
  renderDistribution();
  renderCacheComparison();
  renderStatements();
  renderRecent();
  renderCachePanel();
  renderProfile();
  renderEvents();
  bindChartControls();
  drawTimelineChart();
}

function renderKpis() {
  const profile = state.data_profile;
  const comp = state.comparison;
  const adaptive = state.summaries.adaptive;
  const cache = state.cache;
  document.getElementById('kpiGrid').innerHTML = `
    <div class="kpi blue">
      <div class="label">商户订单量最大 / 最小</div>
      <div class="value">${fmt(profile.size_ratio, 0)}×</div>
      <div class="hint">${fmt(profile.max_orders_per_merchant, 0)} / ${profile.min_orders_per_merchant} 单，幂律 α=${profile.power_law_alpha}</div>
    </div>
    <div class="kpi bad">
      <div class="label">单计划缓存 p95 慢多少</div>
      <div class="value">${comp.single_vs_adaptive_p95_slowdown}×</div>
      <div class="hint">均值慢 ${comp.single_vs_adaptive_mean_slowdown}×；错误计划 ${state.summaries.single.wrong_plan_calls} 次</div>
    </div>
    <div class="kpi good">
      <div class="label">分档缓存选中最优计划比例</div>
      <div class="value">${(adaptive.optimal_plan_match_rate * 100).toFixed(1)}%</div>
      <div class="hint">p95 ${fmt(adaptive.predicted_e2e_ms.p95)} ms，reopt ${fmt(state.summaries.reopt.predicted_e2e_ms.p95)} ms</div>
    </div>
    <div class="kpi purple">
      <div class="label">当前缓存条数 / 上限</div>
      <div class="value">${cache.size}/${cache.max_entries}</div>
      <div class="hint">累计淘汰 ${cache.metrics.evictions}，失效 ${cache.metrics.invalidations}，重启 ${cache.metrics.restarts}</div>
    </div>
  `;
}

function renderDistribution() {
  const rows = Object.entries(STRATEGY_META).map(([key, meta]) => {
    const summary = state.summaries[key];
    const d = summary.predicted_e2e_ms;
    const wall = summary.actual_total_wall_ms;
    return `
      <tr>
        <td><span class="swatch" style="background:${meta.color}"></span> <strong>${meta.name}</strong></td>
        <td class="num">${fmt(d.mean)}</td>
        <td class="num">${fmt(d.p50)}</td>
        <td class="num">${fmt(d.p90)}</td>
        <td class="num"><strong>${fmt(d.p95)}</strong></td>
        <td class="num">${fmt(d.p99)}</td>
        <td class="num bad-text">${fmt(d.max)}</td>
        <td class="num">${fmt(wall.p95)}</td>
        <td class="num">${(summary.optimal_plan_match_rate * 100).toFixed(0)}%</td>
      </tr>`;
  }).join('');
  document.getElementById('distributionTable').innerHTML = `
    <div class="table-wrap">
      <table>
        <thead><tr>
          <th>策略</th><th class="num">均值</th><th class="num">P50</th><th class="num">P90</th>
          <th class="num">P95</th><th class="num">P99</th><th class="num">Max</th>
          <th class="num">实测 P95</th><th class="num">最优率</th>
        </tr></thead>
        <tbody>${rows}</tbody>
      </table>
    </div>
    <p class="hint-note">模型耗时由代价常数 × 实际扫描/回表/连接/排序行数计算；实测 P95 是 Python 执行器墙钟，用于验证方向。</p>
  `;
}

function renderCacheComparison() {
  const c = state.comparison;
  const optCounts = c.optimization_count;
  const optMs = c.total_optimizer_ms;
  const maxCount = Math.max(optCounts.adaptive, optCounts.single, optCounts.reopt, 1);
  const maxMs = Math.max(optMs.adaptive, optMs.single, optMs.reopt, 0.001);
  const bar = (label, value, max, color, suffix = '') => `
    <div class="range-line">
      <div>${label}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.max(2, 100 * value / max)}%;background:${color}"></div></div>
      <div class="num">${fmt(value, value < 10 ? 2 : 0)}${suffix}</div>
    </div>`;
  document.getElementById('cacheCompare').innerHTML = `
    <div class="metric-row">
      <div class="metric-tile"><div class="k">分档缓存当前条数</div><div class="v">${c.cache_entries.adaptive_current}/${c.cache_entries.adaptive_max}</div></div>
      <div class="metric-tile"><div class="k">单计划缓存条数</div><div class="v">${c.cache_entries.single_current}</div></div>
      <div class="metric-tile"><div class="k">分档累计插入/淘汰</div><div class="v">${c.cache_entries.adaptive_inserted}/${c.cache_entries.adaptive_evictions}</div></div>
      <div class="metric-tile"><div class="k">reopt 缓存条数</div><div class="v">0</div></div>
    </div>
    <h4>完整优化/建档次数</h4>
    ${bar('分档缓存', optCounts.adaptive, maxCount, STRATEGY_META.adaptive.color, ' 次')}
    ${bar('单计划', optCounts.single, maxCount, STRATEGY_META.single.color, ' 次')}
    ${bar('每次重优化', optCounts.reopt, maxCount, STRATEGY_META.reopt.color, ' 次')}
    <h4>优化器累计 CPU 耗时</h4>
    ${bar('分档缓存', optMs.adaptive, maxMs, STRATEGY_META.adaptive.color, ' ms')}
    ${bar('单计划', optMs.single, maxMs, STRATEGY_META.single.color, ' ms')}
    ${bar('每次重优化', optMs.reopt, maxMs, STRATEGY_META.reopt.color, ' ms')}
    <p class="hint-note">分档模式首次见到语句时会做直方图 what-if 探测并建立档位；之后调用只计算选择率、查档和 LRU 缓存。</p>
  `;
}

function logX(s) {
  const floor = 1e-6;
  return (Math.log10(Math.max(s, floor)) - Math.log10(floor)) / (0 - Math.log10(floor));
}

function policyButtons(stmt) {
  const key = stmt.statement_id;
  const policy = stmt.policy || { mode: 'auto' };
  const options = stmt.family === 'merchant_orders_join'
    ? [
      ['index_nl', '强制 索引+NL'],
      ['index_hash', '强制 索引+Hash'],
      ['seq_hash', '强制 全表+Hash'],
      ['seq_nl', '强制 全表+NL'],
    ]
    : stmt.family === 'merchant_orders_aggregate'
      ? [['index_agg', '强制索引'], ['seq_agg', '强制全表']]
      : [['pk_point', '强制主键点查'], ['seq_scan', '强制全表']];

  const buttons = options.map(([sig, label]) => `
    <button class="mini ${policy.mode === 'pinned' && policy.forced_signature === sig ? 'active' : ''}"
      data-action="policy" data-sid="${key}" data-mode="pinned" data-sig="${sig}">${label}</button>
  `).join('');
  return `
    <button class="mini good ${policy.mode === 'auto' ? 'active' : ''}"
      data-action="policy" data-sid="${key}" data-mode="auto">恢复自动分档</button>
    ${buttons}
    <button class="mini danger ${policy.mode === 'disabled' ? 'active' : ''}"
      data-action="policy" data-sid="${key}" data-mode="disabled">禁止缓存</button>
  `;
}

function renderStatements() {
  const html = state.statements.map(stmt => {
    const tierColors = {
      index_nl: '#38d39f', index_hash: '#56d4dd', seq_hash: '#ffb454', seq_nl: '#ff8f70',
      index_agg: '#38d39f', seq_agg: '#ffb454', pk_point: '#38d39f', seq_scan: '#ffb454',
    };
    const segments = stmt.tiers.map(tier => {
      const left = logX(tier.lower_selectivity) * 100;
      const right = logX(tier.upper_selectivity) * 100;
      const width = Math.max(0.8, right - left);
      return `<div class="tier-segment" title="${tier.label}: ${pct(tier.lower_selectivity)} ~ ${pct(tier.upper_selectivity)}"
        style="margin-left:${left}%;width:${width}%;background:${tierColors[tier.plan_signature] || '#5b8cff'}"></div>`;
    }).join('');

    const tiers = stmt.tiers.map(tier => `
      <div class="tier ${tier.cached ? 'cached' : 'evicted'}" style="--tier-color:${tierColors[tier.plan_signature] || '#5b8cff'}">
        <div class="stmt-meta">
          ${badge(tier.tier_id.toUpperCase(), 'blue')}
          ${tier.cached ? badge('计划缓存中', 'green') : badge('已淘汰/待重建', 'gray')}
        </div>
        <h4>${tier.label}</h4>
        <div class="range">选择率 ${pct(tier.lower_selectivity, 4)} ≤ s &lt; ${pct(tier.upper_selectivity, 4)}</div>
        <div class="range">代表选择率 ${selectivityText(tier.representative_selectivity)}，约 ${fmt(tier.plan.estimated_rows, 0)} 行</div>
        <div class="plan-name">${planBadge(tier.plan_signature)}</div>
        <div>${tier.plan.label}</div>
        <div class="cost">估算成本 ${fmt(tier.plan.cost_ms, 3)} ms</div>
        <details>
          <summary>查看执行计划 / 成本项</summary>
          <ul>${tier.plan.explain.map(line => `<li>${line}</li>`).join('')}</ul>
          <div class="cache-key">key: ${tier.cache_key ? tier.cache_key.slice(0, 110) + '...' : '-'}</div>
        </details>
      </div>
    `).join('');

    const probes = (stmt.probes || []).slice(0, 18).map(p =>
      `<span class="probe">${p.source}: s=${selectivityText(p.selectivity)} → ${p.winner}</span>`
    ).join('');

    const policy = stmt.policy || {};
    const policyBadge = policy.mode === 'pinned'
      ? badge(`手动锁定 ${policy.forced_signature}`, 'purple')
      : policy.mode === 'disabled'
        ? badge('缓存已禁止', 'red')
        : badge('自动参数敏感分档', 'green');

    return `
      <article class="statement-card" data-sid="${stmt.statement_id}">
        <div class="stmt-head">
          <div>
            <div class="stmt-meta">
              ${badge(stmt.statement_id, 'gray')}
              ${badge(FAMILY_NAMES[stmt.family] || stmt.family, 'blue')}
              ${stmt.sensitive ? badge('识别为参数敏感', 'orange') : badge('参数不敏感', 'green')}
              ${stmt.needs_rebuild ? badge('重启/统计后待重建', 'orange') : ''}
              ${policyBadge}
              <span class="badge gray">调用 ${stmt.call_count}</span>
            </div>
            <div class="sql">${stmt.normalized}</div>
          </div>
          <div class="policy-actions">${policyButtons(stmt)}</div>
        </div>
        <div class="tier-bar">${segments}</div>
        <div class="tier-scale"><span>1e-6 选择率</span><span>对数刻度；颜色=最优算子</span><span>100%</span></div>
        <div class="tiers">${tiers}</div>
        ${probes ? `<div><strong>直方图探测依据：</strong><div class="probe-list">${probes}</div></div>` : ''}
      </article>
    `;
  }).join('');
  document.getElementById('statementList').innerHTML = html;

  document.querySelectorAll('[data-action="policy"]').forEach(button => {
    button.addEventListener('click', async () => {
      try {
        await postJSON('/api/policy', {
          statement_id: button.dataset.sid,
          mode: button.dataset.mode,
          forced_signature: button.dataset.sig || null,
        });
        await refresh(true);
      } catch (err) { alert(err.message); }
    });
  });
}

function renderRecent() {
  const traces = (state.recent_calls || []).slice(0, 36);
  const tbody = document.querySelector('#recentTable tbody');
  tbody.innerHTML = traces.map(t => `
    <tr title="${escapeHtml(t.sql)}">
      <td>${t.seq}</td>
      <td>${badge(t.statement_id, 'gray')}<div class="cache-key">${FAMILY_NAMES[t.family] || t.family}</div></td>
      <td class="selectivity-cell">
        ${selectivityText(t.selectivity)}
        <div>${t.tier_id ? badge(`${t.tier_id} · ${t.tier_label || ''}`, 'blue') : '-'}</div>
        <div class="mini-bar"><i style="width:${Math.min(100, Math.max(2, t.selectivity * 100))}%"></i></div>
      </td>
      <td>${planBadge(t.plan_signature)}<div class="cache-key">${t.access_path}</div></td>
      <td>${statusBadge(t.cache_status)}</td>
      <td class="num">${fmt(t.predicted_total_ms, 3)}<div class="cache-key">代价 ${fmt(t.cost_ms, 2)}</div></td>
      <td class="num">${fmt(t.total_wall_ms, 3)}</td>
    </tr>
  `).join('');
}

function renderCachePanel() {
  const cache = state.cache;
  const m = cache.metrics;
  const entries = cache.entries.map(e => `
    <div class="cache-entry">
      <div class="top">
        <div><strong>${SIGNATURE_NAMES[e.signature] || e.signature}</strong> ${badge(e.tier_id, e.pinned ? 'purple' : 'blue')}</div>
        ${e.pinned ? badge('手动保护', 'purple') : badge(`LRU uses=${e.uses}`, 'gray')}
      </div>
      <div class="cache-key">${e.cache_key}</div>
    </div>
  `).join('');
  document.getElementById('cachePanel').innerHTML = `
    <div class="range-line">
      <div>缓存上限</div>
      <input id="cacheLimit" type="range" min="1" max="12" value="${cache.max_entries}" />
      <div class="num"><span id="cacheLimitValue">${cache.max_entries}</span> 条</div>
    </div>
    <div style="display:flex;gap:8px;margin-bottom:10px">
      <button class="mini primary" id="applyCacheLimit">应用上限并 LRU 淘汰</button>
    </div>
    <div class="metric-row">
      <div class="metric-tile"><div class="k">Hits</div><div class="v">${m.hits}</div></div>
      <div class="metric-tile"><div class="k">Misses</div><div class="v">${m.misses}</div></div>
      <div class="metric-tile"><div class="k">Evictions</div><div class="v">${m.evictions}</div></div>
      <div class="metric-tile"><div class="k">Invalidations</div><div class="v">${m.invalidations}</div></div>
      <div class="metric-tile"><div class="k">Hit ratio</div><div class="v">${(m.hit_ratio * 100).toFixed(1)}%</div></div>
      <div class="metric-tile"><div class="k">Bypasses</div><div class="v">${m.bypasses}</div></div>
    </div>
    <h4>当前 LRU 条目（最新使用在前）</h4>
    ${entries || '<p class="hint-note">缓存为空。</p>'}
  `;
  const slider = document.getElementById('cacheLimit');
  slider?.addEventListener('input', () => {
    document.getElementById('cacheLimitValue').textContent = slider.value;
  });
  document.getElementById('applyCacheLimit')?.addEventListener('click', async () => {
    try {
      await postJSON('/api/cache-limit', { max_entries: Number(slider.value) });
      await refresh(true);
    } catch (err) { alert(err.message); }
  });
}

function renderProfile() {
  const p = state.data_profile;
  const sizeRows = p.merchant_size_distribution.map(item => `
    <div class="range-line">
      <div>${item.range}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.max(2, 100 * item.merchants / p.merchant_count)}%;background:#5b8cff"></div></div>
      <div class="num">${item.merchants}</div>
    </div>`).join('');
  const trafficRows = p.traffic_distribution.map(item => `
    <div class="range-line">
      <div>${item.range}</div>
      <div class="bar-track"><div class="bar-fill" style="width:${Math.max(1, item.traffic_pct)}%;background:#38d39f"></div></div>
      <div class="num">${item.traffic_pct}%</div>
    </div>`).join('');
  document.getElementById('profilePanel').innerHTML = `
    <p><strong>${p.merchant_count}</strong> 个商户、<strong>${p.order_count.toLocaleString()}</strong> 笔订单；大小商户订单量相差 <strong>${p.size_ratio}×</strong>。</p>
    <p class="hint-note">${p.traffic_weight}</p>
    <h4>商户数量按订单规模分布</h4>${sizeRows}
    <h4>真实调用流量分布</h4>${trafficRows}
  `;
}

function renderEvents() {
  const benchmarkEvents = (state.events || []).map(e => `
    <div class="event ${e.type}">
      <div class="when">调用 #${e.seq} 之后</div>
      <strong>${e.title}</strong>
      <div>${e.detail}</div>
    </div>
  `).join('');
  const liveEvents = (state.live_events || []).slice().reverse().map(e => `
    <div class="event ${e.kind}">
      <div class="when">页面手动操作</div>
      <strong>${e.title}</strong>
      <div>${e.detail}</div>
    </div>
  `).join('');
  document.getElementById('eventsPanel').innerHTML = liveEvents + benchmarkEvents;
}

function bindChartControls() {
  const metric = document.getElementById('metricSelect');
  const log = document.getElementById('logScale');
  metric.value = chartOptions.metric;
  log.checked = chartOptions.log;
  metric.addEventListener('change', () => {
    chartOptions.metric = metric.value;
    drawTimelineChart();
  });
  log.addEventListener('change', () => {
    chartOptions.log = log.checked;
    drawTimelineChart();
  });
  document.getElementById('chartLegend').innerHTML = Object.entries(STRATEGY_META).map(([key, meta]) =>
    `<span class="legend-item"><i class="swatch" style="background:${meta.color}"></i>${meta.name}</span>`
  ).join('') + '<span class="legend-item"><i class="swatch" style="background:#ffb454"></i>事件线</span>';
}

function drawTimelineChart() {
  const canvas = document.getElementById('timelineChart');
  if (!canvas || !state.traces) return;
  const rect = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  const width = Math.max(640, rect.width);
  const height = 360;
  canvas.width = width * dpr;
  canvas.height = height * dpr;
  canvas.style.height = `${height}px`;
  const ctx = canvas.getContext('2d');
  ctx.scale(dpr, dpr);
  ctx.clearRect(0, 0, width, height);

  const margin = { top: 18, right: 24, bottom: 42, left: 64 };
  const plotW = width - margin.left - margin.right;
  const plotH = height - margin.top - margin.bottom;
  const allTraces = state.traces;
  const count = state.config.calls;
  const metric = chartOptions.metric;

  let maxValue = 0.1;
  Object.values(allTraces).forEach(list => list.forEach(t => { maxValue = Math.max(maxValue, t[metric] || 0); }));
  maxValue *= 1.08;
  const minLog = -3; // 0.001 ms floor
  const yValue = v => {
    if (chartOptions.log) {
      const lv = Math.log10(Math.max(0.001, v));
      const maxLog = Math.log10(maxValue);
      return margin.top + plotH * (1 - (lv - minLog) / Math.max(0.01, maxLog - minLog));
    }
    return margin.top + plotH * (1 - v / maxValue);
  };
  const xValue = seq => margin.left + plotW * ((seq - 1) / Math.max(1, count - 1));

  ctx.strokeStyle = 'rgba(147,164,199,.18)';
  ctx.fillStyle = '#93a4c7';
  ctx.font = '11px ui-monospace, monospace';
  ctx.lineWidth = 1;
  const ticks = 5;
  for (let i = 0; i <= ticks; i++) {
    const y = margin.top + plotH * i / ticks;
    ctx.beginPath(); ctx.moveTo(margin.left, y); ctx.lineTo(margin.left + plotW, y); ctx.stroke();
    let label;
    if (chartOptions.log) {
      const maxLog = Math.log10(maxValue);
      const lv = minLog + (maxLog - minLog) * (1 - i / ticks);
      label = `${Math.pow(10, lv).toFixed(lv < 0 ? 3 : lv < 1 ? 2 : 0)}`;
    } else {
      label = fmt(maxValue * (1 - i / ticks), 1);
    }
    ctx.fillText(label, 12, y + 4);
  }
  for (let i = 0; i <= 6; i++) {
    const seq = Math.round(1 + (count - 1) * i / 6);
    const x = xValue(seq);
    ctx.fillText(`#${seq}`, x - 14, height - 16);
  }

  (state.events || []).forEach(event => {
    const x = xValue(event.seq);
    ctx.setLineDash([5, 5]);
    ctx.strokeStyle = event.type === 'restart' ? 'rgba(255,180,84,.75)' : 'rgba(56,211,159,.75)';
    ctx.beginPath(); ctx.moveTo(x, margin.top); ctx.lineTo(x, margin.top + plotH); ctx.stroke();
    ctx.setLineDash([]);
    ctx.save();
    ctx.translate(x + 4, margin.top + 12);
    ctx.fillStyle = event.type === 'restart' ? '#ffb454' : '#55e0b2';
    ctx.font = '11px sans-serif';
    ctx.fillText(event.type === 'restart' ? '重启' : '统计更新', 0, 0);
    ctx.restore();
  });

  Object.entries(STRATEGY_META).forEach(([key, meta]) => {
    const list = allTraces[key] || [];
    ctx.strokeStyle = meta.color;
    ctx.lineWidth = key === 'adaptive' ? 2.2 : 1.5;
    ctx.globalAlpha = key === 'adaptive' ? 1 : 0.72;
    ctx.beginPath();
    list.forEach((t, i) => {
      const x = xValue(t.seq);
      const y = yValue(t[metric] || 0);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    ctx.globalAlpha = 1;
  });

  chartModel = { margin, plotW, plotH, count, xValue, yValue, metric };
  canvas.onmousemove = event => {
    const rect2 = canvas.getBoundingClientRect();
    const x = event.clientX - rect2.left;
    if (x < margin.left || x > margin.left + plotW) {
      document.getElementById('tooltip').classList.add('hidden');
      return;
    }
    const ratio = (x - margin.left) / plotW;
    const seq = Math.max(1, Math.min(count, Math.round(1 + ratio * (count - 1))));
    const tip = document.getElementById('tooltip');
    const rows = Object.entries(STRATEGY_META).map(([key, meta]) => {
      const t = (state.traces[key] || []).find(item => item.seq === seq);
      return t ? `<div><span style="color:${meta.color}">●</span> ${meta.name}: <strong>${fmt(t[metric], 3)}</strong> ms<br><span style="color:#93a4c7">${t.plan_signature} · ${t.cache_status}</span></div>` : '';
    }).join('');
    tip.innerHTML = `<strong>调用 #${seq}</strong>${rows}`;
    tip.style.left = `${Math.min(window.innerWidth - 300, event.clientX + 14)}px`;
    tip.style.top = `${event.clientY + 14}px`;
    tip.classList.remove('hidden');
  };
  canvas.onmouseleave = () => document.getElementById('tooltip').classList.add('hidden');
}

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, ch => ({
    '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'
  }[ch]));
}

document.getElementById('refreshBtn').addEventListener('click', () => refresh(true));
document.getElementById('callMainBtn').addEventListener('click', async () => {
  const merchant_id = Number(document.getElementById('merchantId').value || 1);
  try {
    await postJSON('/api/call', { kind: 'main', merchant_id });
    await refresh(true);
  } catch (err) { alert(err.message); }
});
document.getElementById('callAggBtn').addEventListener('click', async () => {
  const merchant_id = Number(document.getElementById('merchantId').value || 1);
  try {
    await postJSON('/api/call', { kind: 'agg', merchant_id });
    await refresh(true);
  } catch (err) { alert(err.message); }
});
document.getElementById('restartBtn').addEventListener('click', async () => {
  try {
    await postJSON('/api/restart', {});
    await refresh(true);
  } catch (err) { alert(err.message); }
});
document.getElementById('statsBtn').addEventListener('click', async () => {
  try {
    await postJSON('/api/refresh-stats', {});
    await refresh(true);
  } catch (err) { alert(err.message); }
});
document.getElementById('burstStatsBtn').addEventListener('click', async () => {
  const merchant_id = Number(document.getElementById('merchantId').value || 1);
  try {
    await postJSON('/api/refresh-stats', { merchant_id, burst_size: 5000 });
    await refresh(true);
  } catch (err) { alert(err.message); }
});

window.addEventListener('resize', () => { if (state) drawTimelineChart(); });
refresh(true);
