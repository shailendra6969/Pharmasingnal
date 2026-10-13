/**
 * PharmaSignal Client Interactivity
 * Pure Vanilla JS: SVG Chart rendering, autocomplete, dynamic filters, sensitivity toggle
 */

document.addEventListener('DOMContentLoaded', () => {
  initSearchAutocomplete();
  initTrendCharts();
  initDemographicCharts();
});

// ============================================================================
// Autocomplete Search for Drugs & Events
// ============================================================================
function initSearchAutocomplete() {
  const drugInput = document.getElementById('search-drug-input');
  const drugList = document.getElementById('drug-autocomplete-list');
  const eventInput = document.getElementById('search-event-input');
  const eventList = document.getElementById('event-autocomplete-list');

  if (drugInput && drugList) {
    setupAutocomplete(drugInput, drugList, '/api/search/drugs?q=', item => item.drug);
  }
  if (eventInput && eventList) {
    setupAutocomplete(eventInput, eventList, '/api/search/events?q=', item => item.event);
  }
}

function setupAutocomplete(inputEl, listEl, apiEndpoint, getLabel) {
  let debounceTimer;

  inputEl.addEventListener('input', e => {
    const val = e.target.value.trim();
    clearTimeout(debounceTimer);
    if (val.length < 2) {
      listEl.innerHTML = '';
      listEl.style.display = 'none';
      return;
    }

    debounceTimer = setTimeout(async () => {
      try {
        const res = await fetch(`${apiEndpoint}${encodeURIComponent(val)}&limit=8`);
        if (!res.ok) return;
        const data = await res.json();
        const items = data.items || [];

        if (items.length === 0) {
          listEl.style.display = 'none';
          return;
        }

        listEl.innerHTML = items
          .map(
            it => `
            <div class="autocomplete-item" data-value="${getLabel(it)}">
              <span class="ac-title">${getLabel(it)}</span>
              <span class="ac-badge">${it.max_cases} cases</span>
            </div>
          `
          )
          .join('');
        listEl.style.display = 'block';

        listEl.querySelectorAll('.autocomplete-item').forEach(item => {
          item.addEventListener('click', () => {
            inputEl.value = item.getAttribute('data-value');
            listEl.style.display = 'none';
          });
        });
      } catch (err) {
        console.error('Autocomplete error:', err);
      }
    }, 250);
  });

  document.addEventListener('click', e => {
    if (!inputEl.contains(e.target) && !listEl.contains(e.target)) {
      listEl.style.display = 'none';
    }
  });
}

// ============================================================================
// SVG Trend Line Chart Generator
// ============================================================================
function initTrendCharts() {
  const chartContainers = document.querySelectorAll('[data-chart="quarterly-trend"]');
  chartContainers.forEach(container => {
    try {
      const rawData = container.getAttribute('data-points');
      if (!rawData) return;
      const points = JSON.parse(rawData);
      if (!Array.isArray(points) || points.length === 0) {
        container.innerHTML = '<div style="color:var(--text-muted);padding:2rem;text-align:center;">No quarterly trend data recorded.</div>';
        return;
      }
      renderSvgLineChart(container, points);
    } catch (e) {
      console.error('Failed to parse chart data', e);
    }
  });
}

function renderSvgLineChart(container, data) {
  const width = container.clientWidth || 600;
  const height = container.clientHeight || 200;
  const padding = { top: 20, right: 30, bottom: 40, left: 55 };

  const innerWidth = width - padding.left - padding.right;
  const innerHeight = height - padding.top - padding.bottom;

  const yValues = data.map(d => Number(d.a !== undefined ? d.a : d.cases !== undefined ? d.cases : d.reports || 0));
  const maxY = Math.max(...yValues, 10) * 1.15;
  const minY = 0;

  const getX = idx => padding.left + (idx / Math.max(1, data.length - 1)) * innerWidth;
  const getY = val => padding.top + innerHeight - ((val - minY) / (maxY - minY)) * innerHeight;

  // Build SVG path
  const lineCoords = data.map((d, i) => `${getX(i)},${getY(yValues[i])}`);
  const linePath = `M ${lineCoords.join(' L ')}`;
  const areaPath = `M ${getX(0)},${getY(0)} L ${lineCoords.join(' L ')} L ${getX(data.length - 1)},${getY(0)} Z`;

  let pointsSvg = data
    .map((d, i) => {
      const cx = getX(i);
      const cy = getY(yValues[i]);
      const label = d.quarter || '';
      const val = yValues[i];
      return `
        <circle cx="${cx}" cy="${cy}" r="5" fill="#06b6d4" stroke="#ffffff" stroke-width="2">
          <title>${label}: ${val} cases</title>
        </circle>
        <text x="${cx}" y="${height - 15}" text-anchor="middle" font-size="11" fill="#94a3b8" font-family="var(--font-mono)">${label}</text>
      `;
    })
    .join('');

  // Horizontal Grid Lines
  const gridTicks = [0, maxY * 0.33, maxY * 0.66, maxY];
  let gridSvg = gridTicks
    .map(t => {
      const yPos = getY(t);
      return `
        <line x1="${padding.left}" y1="${yPos}" x2="${width - padding.right}" y2="${yPos}" stroke="rgba(255,255,255,0.06)" stroke-dasharray="3 3" />
        <text x="${padding.left - 8}" y="${yPos + 4}" text-anchor="end" font-size="10" fill="#64748b" font-family="var(--font-mono)">${Math.round(t)}</text>
      `;
    })
    .join('');

  container.innerHTML = `
    <svg width="${width}" height="${height}" viewBox="0 0 ${width} ${height}" style="overflow:visible;">
      <defs>
        <linearGradient id="areaGrad" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stop-color="#06b6d4" stop-opacity="0.35"/>
          <stop offset="100%" stop-color="#06b6d4" stop-opacity="0.0"/>
        </linearGradient>
      </defs>
      ${gridSvg}
      <path d="${areaPath}" fill="url(#areaGrad)"/>
      <path d="${linePath}" fill="none" stroke="#06b6d4" stroke-width="3" stroke-linecap="round"/>
      ${pointsSvg}
    </svg>
  `;
}

// ============================================================================
// Horizontal Bar Visualizer for Demographics
// ============================================================================
function initDemographicCharts() {
  document.querySelectorAll('[data-chart="bar-distribution"]').forEach(container => {
    try {
      const rawData = container.getAttribute('data-distribution');
      if (!rawData) return;
      const items = JSON.parse(rawData);
      if (!Array.isArray(items) || items.length === 0) return;

      const total = items.reduce((acc, it) => acc + (it.cases || 0), 0);
      if (total === 0) return;

      container.innerHTML = items
        .map(it => {
          const count = it.cases || 0;
          const pct = ((count / total) * 100).toFixed(1);
          const label = it.age_group || it.sex || it.role || 'Other';
          return `
            <div style="margin-bottom: 0.75rem;">
              <div style="display:flex; justify-content:space-between; font-size:0.8rem; margin-bottom:0.25rem;">
                <span style="color:var(--text-primary); font-weight:500;">${label}</span>
                <span style="color:var(--text-muted); font-family:var(--font-mono);">${count.toLocaleString()} (${pct}%)</span>
              </div>
              <div style="height:8px; background:rgba(255,255,255,0.06); border-radius:999px; overflow:hidden;">
                <div style="height:100%; width:${pct}%; background:linear-gradient(90deg, #06b6d4, #8b5cf6); border-radius:999px; transition:width 0.8s ease;"></div>
              </div>
            </div>
          `;
        })
        .join('');
    } catch (e) {
      console.error('Failed to parse distribution data', e);
    }
  });
}
