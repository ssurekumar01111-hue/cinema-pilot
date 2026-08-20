// CinemaPilot Dashboard Frontend Application

let currentTab = 'scenes';
let scenes = [];
let activeSceneId = null;
let auditEvents = [];

if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', () => {
    initDashboard();
  });
} else {
  initDashboard();
}

async function initDashboard() {
  await Promise.all([
    loadScenes(),
    loadEvents()
  ]);
}

function switchTab(tabName) {
  currentTab = tabName;
  const scenesTabBtn = document.getElementById('tab-scenes-btn');
  const timelineTabBtn = document.getElementById('tab-timeline-btn');
  const sceneContainer = document.getElementById('scene-detail-container');
  const timelineContainer = document.getElementById('timeline-view-container');

  if (tabName === 'scenes') {
    scenesTabBtn.classList.add('active');
    timelineTabBtn.classList.remove('active');
    sceneContainer.style.display = 'block';
    timelineContainer.style.display = 'none';
  } else {
    scenesTabBtn.classList.remove('active');
    timelineTabBtn.classList.add('active');
    sceneContainer.style.display = 'none';
    timelineContainer.style.display = 'block';
    renderTimeline();
  }
}

async function refreshCurrentView() {
  const refreshBtn = document.getElementById('refresh-btn');
  refreshBtn.classList.add('spinning');
  
  await Promise.all([
    loadScenes(),
    loadEvents()
  ]);

  if (activeSceneId) {
    await loadSceneDetail(activeSceneId);
  }

  setTimeout(() => {
    refreshBtn.classList.remove('spinning');
  }, 400);
}

// ----------------------------------------------------------------------------
// SCENES LIST
// ----------------------------------------------------------------------------

async function loadScenes() {
  const scenesListEl = document.getElementById('scenes-list');
  const sceneCounterEl = document.getElementById('scene-counter');

  try {
    const res = await fetch('/api/scenes');
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    scenes = await res.json();

    sceneCounterEl.textContent = `${scenes.length} Scenes`;
    renderScenesList();

    // Auto-select scene_005 by default if available, or the first scene
    if (!activeSceneId && scenes.length > 0) {
      const preferred = scenes.find(s => s.scene_id === 'scene_005') || scenes[0];
      selectScene(preferred.scene_id);
    }
  } catch (err) {
    scenesListEl.innerHTML = `
      <div class="empty-state">
        <span style="color: var(--accent-rose);">⚠️ Failed to load scenes</span>
        <small>${escapeHtml(err.message)}</small>
      </div>
    `;
  }
}

function renderScenesList() {
  const scenesListEl = document.getElementById('scenes-list');
  if (scenes.length === 0) {
    scenesListEl.innerHTML = '<div class="empty-state">No scenes found in Production Graph.</div>';
    return;
  }

  scenesListEl.innerHTML = scenes.map(s => {
    const isActive = s.scene_id === activeSceneId;
    const tone = s.emotional_tone || 'standard';
    return `
      <div class="scene-card-item ${isActive ? 'active' : ''}" onclick="selectScene('${s.scene_id}')">
        <div class="scene-card-top">
          <span class="scene-number-label">Scene ${s.scene_number || s.scene_id.replace('scene_', '')}</span>
          <span class="badge badge-blue">${escapeHtml(s.status || 'draft')}</span>
        </div>
        <div class="scene-card-location" title="${escapeHtml(s.location_name || s.location_id || '')}">
          📍 ${escapeHtml(s.location_name || s.location_id || 'Unassigned')}
        </div>
        <div class="scene-card-bottom">
          <span class="badge badge-purple">${escapeHtml(tone)}</span>
          <span style="font-size: 0.75rem; color: var(--text-dim);">Pos #${s.timeline_position}</span>
        </div>
      </div>
    `;
  }).join('');
}

function selectScene(sceneId) {
  activeSceneId = sceneId;
  renderScenesList();
  loadSceneDetail(sceneId);
}

// ----------------------------------------------------------------------------
// SCENE DETAIL
// ----------------------------------------------------------------------------

async function loadSceneDetail(sceneId) {
  const container = document.getElementById('scene-detail-container');
  container.innerHTML = `
    <div class="loading-state">
      <div class="spinner"></div>
      <span>Loading Scene ${escapeHtml(sceneId)} Production Graph...</span>
    </div>
  `;

  try {
    const res = await fetch(`/api/scene/${sceneId}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}: ${res.statusText}`);
    const data = await res.json();
    renderSceneDetail(data);
  } catch (err) {
    container.innerHTML = `
      <div class="empty-state">
        <span style="color: var(--accent-rose);">⚠️ Error loading scene detail</span>
        <small>${escapeHtml(err.message)}</small>
      </div>
    `;
  }
}

function renderSceneDetail(data) {
  const {
    scene,
    location,
    characters,
    props,
    budget_lines,
    schedule_blocks,
    risk_flags,
    storyboard,
    music_cue,
    director_note,
    producer_overview,
    explanation,
  } = data;

  const container = document.getElementById('scene-detail-container');

  const locName = location ? location.name : (scene.location_id || 'Unassigned Location');
  const locType = location ? location.location_type : 'Unknown';
  const weatherSens = location && location.weather_sensitivity ? '🌧️ Weather Sensitive' : '☀️ Standard Weather';
  const cameraCues = (scene.camera_cues || []).join(', ') || 'Standard framing';

  // Build characters HTML
  let charsHtml = '<span style="color: var(--text-dim); font-size: 0.8rem;">No characters assigned</span>';
  if (characters && characters.length > 0) {
    charsHtml = characters.map(c => `
      <div class="char-pill">
        <strong>${escapeHtml(c.name || c.character_id)}</strong>
        ${c.description ? `<div class="char-desc">${escapeHtml(c.description)}</div>` : ''}
      </div>
    `).join('');
  }

  // Build props HTML
  let propsHtml = '<span style="color: var(--text-dim); font-size: 0.8rem;">No props assigned</span>';
  if (props && props.length > 0) {
    propsHtml = props.map(p => `
      <div class="prop-pill">
        <strong>📦 ${escapeHtml(p.name || p.prop_id)}</strong>
        ${p.sourcing_notes ? `<div class="char-desc">${escapeHtml(p.sourcing_notes)}</div>` : ''}
      </div>
    `).join('');
  }

  // Build Explanation section if present
  let explanationHtml = '';
  if (explanation && explanation.narrative) {
    const sourcesBadges = (explanation.sources_used || []).map(src => `
      <span class="badge badge-blue">🤖 ${escapeHtml(src)}</span>
    `).join('');

    explanationHtml = `
      <section class="explanation-section">
        <div class="explanation-header">
          <div class="explanation-title">
            <span>✨ The Story of the Cascade</span>
          </div>
          <span class="badge badge-purple">Explanation Agent</span>
        </div>
        <blockquote class="explanation-narrative">
          "${escapeHtml(explanation.narrative)}"
        </blockquote>
        <div class="explanation-sources">
          <span style="font-size: 0.8rem; color: var(--text-dim);">Sources Synthesized:</span>
          ${sourcesBadges}
        </div>
      </section>
    `;
  }

  // Build Storyboard Card
  let storyboardHtml = `
    <div class="cascade-card">
      <div class="cascade-card-header">
        <div class="cascade-card-title">🎨 Storyboard Panel</div>
        <span class="badge badge-amber">Pending</span>
      </div>
      <div class="storyboard-media-container">
        <span style="color: var(--text-dim); font-size: 0.85rem;">No storyboard generated yet</span>
      </div>
    </div>
  `;
  const sbMediaUrl = storyboard ? (storyboard.media_url || storyboard.proxy_url || storyboard.signed_url) : null;
  if (storyboard && sbMediaUrl) {
    storyboardHtml = `
      <div class="cascade-card">
        <div class="cascade-card-header">
          <div class="cascade-card-title">🎨 Storyboard Panel</div>
          <span class="badge badge-emerald">Generated (gemini-3.1-flash-image)</span>
        </div>
        <div class="storyboard-media-container">
          <a href="${sbMediaUrl}" target="_blank" title="Click to view full resolution">
            <img src="${sbMediaUrl}" alt="Storyboard for ${escapeHtml(scene.scene_id)}" loading="lazy" />
          </a>
        </div>
        ${storyboard.prompt_used ? `
          <div class="prompt-details">
            <strong style="color: var(--accent-blue);">Prompt:</strong> ${escapeHtml(storyboard.prompt_used)}
          </div>
        ` : ''}
      </div>
    `;
  }

  // Build Music Card
  let musicHtml = `
    <div class="cascade-card">
      <div class="cascade-card-header">
        <div class="cascade-card-title">🎵 Musical Score Cue</div>
        <span class="badge badge-amber">Pending</span>
      </div>
      <div class="empty-state" style="padding: 20px;">
        <span>No music cue generated yet</span>
      </div>
    </div>
  `;
  const mcMediaUrl = music_cue ? (music_cue.media_url || music_cue.proxy_url || music_cue.signed_url) : null;
  if (music_cue && mcMediaUrl) {
    musicHtml = `
      <div class="cascade-card">
        <div class="cascade-card-header">
          <div class="cascade-card-title">🎵 Musical Score Cue</div>
          <span class="badge badge-emerald">Lyria 3 Audio</span>
        </div>
        <div class="music-player-container">
          <audio controls preload="metadata">
            <source src="${mcMediaUrl}" type="audio/mpeg">
            Your browser does not support the audio player.
          </audio>
          ${music_cue.description ? `
            <div class="music-description">${escapeHtml(music_cue.description)}</div>
          ` : ''}
          ${music_cue.prompt_used ? `
            <div class="prompt-details">
              <strong style="color: var(--accent-cyan);">Prompt:</strong> ${escapeHtml(music_cue.prompt_used)}
            </div>
          ` : ''}
        </div>
      </div>
    `;
  }

  // Build Risk Flags Card
  let riskHtml = `
    <div class="cascade-card">
      <div class="cascade-card-header">
        <div class="cascade-card-title">⚠️ Risk Assessment</div>
        <span class="badge badge-emerald">No Active Risks</span>
      </div>
      <p style="color: var(--text-muted); font-size: 0.85rem;">All location and logistical factors clear.</p>
    </div>
  `;
  if (risk_flags && risk_flags.length > 0) {
    const riskItems = risk_flags.map(rf => {
      const sev = (rf.severity || 'medium').toLowerCase();
      const sevBadge = sev === 'high' ? 'badge-rose' : sev === 'medium' ? 'badge-amber' : 'badge-blue';
      let incidentLink = rf.grafana_incident_url || '';
      if (incidentLink) {
        try {
          const parsed = JSON.parse(incidentLink);
          if (parsed.overviewURL) {
            incidentLink = 'https://daringhamster1557.grafana.net' + parsed.overviewURL;
          } else if (parsed.url || parsed.html_url) {
            incidentLink = parsed.url || parsed.html_url;
          }
        } catch (e) {
          // already a direct url
        }
      }

      const incidentBtn = incidentLink ? `
        <a href="${escapeHtml(incidentLink)}" target="_blank" rel="noopener noreferrer" class="grafana-btn">
          🔥 View Grafana Incident ↗
        </a>
      ` : '';

      return `
        <div class="risk-item severity-${sev}">
          <div class="risk-item-header">
            <span class="badge ${sevBadge}">${rf.severity || 'Risk'}</span>
            ${incidentBtn}
          </div>
          <div style="font-size: 0.9rem; color: #fff; font-weight: 600;">
            ${escapeHtml(rf.description || 'No description')}
          </div>
          ${rf.mitigation ? `
            <div style="font-size: 0.85rem; color: #cbd5e1; background: rgba(0,0,0,0.2); padding: 8px 10px; border-radius: 6px;">
              <strong style="color: var(--accent-emerald);">Mitigation:</strong> ${escapeHtml(rf.mitigation)}
            </div>
          ` : '<span style="color: var(--accent-rose); font-size: 0.75rem;">⚠️ Unmitigated</span>'}
        </div>
      `;
    }).join('');

    riskHtml = `
      <div class="cascade-card">
        <div class="cascade-card-header">
          <div class="cascade-card-title">⚠️ Risk Assessment</div>
          <span class="badge badge-rose">${risk_flags.length} Flagged</span>
        </div>
        <div style="display: flex; flex-direction: column; gap: 10px;">
          ${riskItems}
        </div>
      </div>
    `;
  }

  // Build Budget Card
  let budgetHtml = '';
  if (budget_lines && budget_lines.length > 0) {
    const totalAmount = budget_lines.reduce((acc, bl) => acc + (bl.amount || 0), 0);
    const blList = budget_lines.map(bl => `
      <div style="margin-top: 6px; padding-top: 6px; border-top: 1px solid var(--border-subtle);">
        <div style="display: flex; justify-content: space-between; font-size: 0.85rem;">
          <span style="font-weight: 600; color: #fff;">${escapeHtml(bl.category || 'General')}</span>
          <span style="color: var(--accent-emerald); font-weight: 700;">$${Number(bl.amount || 0).toLocaleString()}</span>
        </div>
        <div class="budget-reason">${escapeHtml(bl.reason || '')}</div>
        <small style="color: var(--text-dim); font-size: 0.7rem;">Agent: ${escapeHtml(bl.last_changed_by_agent || 'budget_agent')}</small>
      </div>
    `).join('');

    budgetHtml = `
      <div class="cascade-card">
        <div class="cascade-card-header">
          <div class="cascade-card-title">💰 Budget Impact</div>
          <span class="badge badge-emerald">Budget Agent</span>
        </div>
        <div class="budget-amount">$${totalAmount.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}</div>
        <div>${blList}</div>
      </div>
    `;
  }

  // Build Schedule Card
  let scheduleHtml = '';
  if (schedule_blocks && schedule_blocks.length > 0) {
    const sbList = schedule_blocks.map(sb => {
      const constraints = (sb.constraints || []).map(c => `
        <span class="badge badge-indigo" style="font-size: 0.7rem;">${escapeHtml(c)}</span>
      `).join('');

      return `
        <div style="margin-top: 8px; padding-top: 8px; border-top: 1px solid var(--border-subtle);">
          <div style="display: flex; justify-content: space-between; align-items: center;">
            <strong style="color: #fff;">Day ${sb.day_index || 1}</strong>
            <span style="color: var(--accent-blue); font-size: 0.85rem;">⏱️ ${sb.duration_minutes || 0} minutes</span>
          </div>
          <div style="display: flex; flex-wrap: wrap; gap: 4px; margin-top: 8px;">
            ${constraints || '<span style="color: var(--text-dim); font-size: 0.75rem;">No specific constraints</span>'}
          </div>
        </div>
      `;
    }).join('');

    scheduleHtml = `
      <div class="cascade-card">
        <div class="cascade-card-header">
          <div class="cascade-card-title">📅 Production Schedule</div>
          <span class="badge badge-indigo">Schedule Agent</span>
        </div>
        <div>${sbList}</div>
      </div>
    `;
  }

  // Build Director Notes Card
  let directorHtml = '';
  if (director_note) {
    const shotList = (director_note.shot_suggestions || []).map(s => `
      <li style="margin-bottom: 6px; color: #cbd5e1; font-size: 0.85rem;">${escapeHtml(s)}</li>
    `).join('');

    directorHtml = `
      <div class="cascade-card">
        <div class="cascade-card-header">
          <div class="cascade-card-title">🎬 Director Guidance</div>
          <span class="badge badge-purple">Director Agent</span>
        </div>
        ${director_note.pacing_notes ? `
          <div>
            <strong style="font-size: 0.8rem; color: var(--accent-blue); text-transform: uppercase;">Pacing Notes</strong>
            <p style="font-size: 0.85rem; color: #cbd5e1; margin-top: 4px;">${escapeHtml(director_note.pacing_notes)}</p>
          </div>
        ` : ''}
        ${director_note.camera_plan ? `
          <div style="margin-top: 8px;">
            <strong style="font-size: 0.8rem; color: var(--accent-cyan); text-transform: uppercase;">Camera Plan</strong>
            <p style="font-size: 0.85rem; color: #cbd5e1; margin-top: 4px;">${escapeHtml(director_note.camera_plan)}</p>
          </div>
        ` : ''}
        ${shotList ? `
          <div style="margin-top: 8px;">
            <strong style="font-size: 0.8rem; color: var(--text-dim); text-transform: uppercase;">Shot Suggestions</strong>
            <ul style="padding-left: 18px; margin-top: 6px;">${shotList}</ul>
          </div>
        ` : ''}
      </div>
    `;
  }

  // Build Producer Overview Card
  let producerHtml = '';
  if (producer_overview) {
    producerHtml = `
      <div class="cascade-card">
        <div class="cascade-card-header">
          <div class="cascade-card-title">📋 Executive Producer Overview</div>
          <span class="badge badge-amber">Producer Agent</span>
        </div>
        <p style="font-size: 0.88rem; color: #f1f5f9; line-height: 1.6;">${escapeHtml(producer_overview.overview_summary || '')}</p>
        ${producer_overview.recommendation ? `
          <div style="background: rgba(245, 158, 11, 0.1); border-left: 3px solid var(--accent-amber); padding: 8px 12px; border-radius: 4px; font-size: 0.82rem; color: #fde68a;">
            <strong>Recommendation:</strong> ${escapeHtml(producer_overview.recommendation)}
          </div>
        ` : ''}
      </div>
    `;
  }

  container.innerHTML = `
    <!-- Header -->
    <div class="scene-detail-header">
      <div class="scene-header-top">
        <div class="scene-title-row">
          <h1 class="scene-main-title">Scene ${scene.scene_number || scene.scene_id.replace('scene_', '')}: ${escapeHtml(locName)}</h1>
          <div class="scene-subtitle">Production Node: ${escapeHtml(scene.scene_id)} • Timeline Pos #${scene.timeline_position}</div>
        </div>
        <div class="scene-meta-badges">
          <span class="badge badge-blue">Tone: ${escapeHtml(scene.emotional_tone || 'Standard')}</span>
          <span class="badge badge-purple">${escapeHtml(locType)}</span>
          <span class="badge badge-emerald">${escapeHtml(weatherSens)}</span>
        </div>
      </div>
    </div>

    <!-- Production Context -->
    <div class="context-grid">
      <div class="context-card">
        <div class="card-title-sm">📍 Location Details</div>
        <div style="font-size: 0.9rem; font-weight: 700; color: #fff;">${escapeHtml(locName)}</div>
        <div style="font-size: 0.82rem; color: var(--text-muted);">${escapeHtml(location ? location.logistics_notes || 'No logistics notes' : 'Location record not found')}</div>
        ${location && location.cost_profile ? `
          <div style="font-size: 0.78rem; color: var(--accent-emerald);">Base Cost Profile: $${Number(location.cost_profile).toLocaleString()}</div>
        ` : ''}
      </div>

      <div class="context-card">
        <div class="card-title-sm">👥 Cast Present (${characters ? characters.length : 0})</div>
        <div class="pill-list">${charsHtml}</div>
      </div>

      <div class="context-card">
        <div class="card-title-sm">📦 Required Props (${props ? props.length : 0})</div>
        <div class="pill-list">${propsHtml}</div>
      </div>
    </div>

    <!-- Explanation Hero Story Section -->
    ${explanationHtml}

    <!-- Cascade Heading -->
    <div class="cascade-section-heading">
      <span>⚡ Multi-Agent Cascade Results</span>
    </div>

    <!-- Cascade Cards Grid -->
    <div class="cascade-grid">
      ${storyboardHtml}
      ${musicHtml}
      ${riskHtml}
      ${budgetHtml}
      ${scheduleHtml}
      ${directorHtml}
      ${producerHtml}
    </div>
  `;
}

// ----------------------------------------------------------------------------
// TIMELINE VIEW
// ----------------------------------------------------------------------------

async function loadEvents() {
  const countBadge = document.getElementById('events-count-badge');
  try {
    const res = await fetch('/api/events');
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    auditEvents = await res.json();
    countBadge.textContent = auditEvents.length;
    if (currentTab === 'timeline') {
      renderTimeline();
    }
  } catch (err) {
    console.error('Failed to load events:', err);
  }
}

function renderTimeline() {
  const container = document.getElementById('timeline-events-list');
  if (!auditEvents || auditEvents.length === 0) {
    container.innerHTML = '<div class="empty-state">No audit cascade events recorded yet.</div>';
    return;
  }

  // Sort chronological
  const sorted = [...auditEvents].reverse();

  container.innerHTML = sorted.map((ev, idx) => {
    const triggered = (ev.triggered_agents || []).map(ag => `
      <span class="badge badge-emerald" style="font-size: 0.72rem;">${escapeHtml(ag)}</span>
    `).join('');

    let beforeParsed = ev.before_state;
    let afterParsed = ev.after_state;
    try { if (typeof beforeParsed === 'string') beforeParsed = JSON.parse(beforeParsed); } catch(e) {}
    try { if (typeof afterParsed === 'string') afterParsed = JSON.parse(afterParsed); } catch(e) {}

    return `
      <div class="timeline-event-card">
        <div class="timeline-event-top">
          <div class="timeline-flow">
            <span class="badge badge-purple" style="font-size: 0.8rem;">🤖 ${escapeHtml(ev.actor_agent || 'unknown_agent')}</span>
            <span class="timeline-arrow">→</span>
            <span class="badge badge-blue" style="font-size: 0.8rem;">${escapeHtml(ev.entity_type || '')}: ${escapeHtml(ev.entity_id || '')}</span>
            ${triggered ? `
              <span class="timeline-arrow">→</span>
              <div style="display: flex; gap: 4px; flex-wrap: wrap;">${triggered}</div>
            ` : ''}
          </div>
          <span style="font-size: 0.75rem; color: var(--text-dim); font-family: var(--font-mono);">
            ${escapeHtml(ev.event_timestamp || '')}
          </span>
        </div>

        <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin-top: 4px;">
          <div>
            <span style="font-size: 0.72rem; color: var(--text-dim); text-transform: uppercase;">Before State</span>
            <pre class="timeline-state-box">${escapeHtml(JSON.stringify(beforeParsed, null, 2))}</pre>
          </div>
          <div>
            <span style="font-size: 0.72rem; color: var(--accent-emerald); text-transform: uppercase;">After State</span>
            <pre class="timeline-state-box">${escapeHtml(JSON.stringify(afterParsed, null, 2))}</pre>
          </div>
        </div>
      </div>
    `;
  }).join('');
}

function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#039;');
}
