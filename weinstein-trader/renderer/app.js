/* ── State ──────────────────────────────────────────────────────────────── */
const FLASK = 'http://127.0.0.1:5050';

let portfolioData = { positions: [], account: {} };
let cashData = { settled_cash: 0, net_liquidation: 0 };
let chatHistory = [];
let isStreaming = false;

/* ── Init ───────────────────────────────────────────────────────────────── */
document.addEventListener('DOMContentLoaded', () => {
  checkStatus();
  refreshPortfolio();
  loadWishlist();
  setInterval(checkStatus, 10000);
});

function switchTab(name) {
  document.querySelectorAll('.tab').forEach(t => t.classList.toggle('active', t.dataset.tab === name));
  document.querySelectorAll('.tab-pane').forEach(p => p.classList.toggle('active', p.id === `tab-${name}`));
  if (name === 'nextmove') populateNextMove();
}

async function checkStatus() {
  try {
    const res = await fetch(`${FLASK}/api/status`);
    const data = await res.json();
    const dot = document.getElementById('conn-dot');
    const label = document.getElementById('conn-label');
    if (data.connected) {
      dot.className = 'conn-dot connected';
      label.textContent = 'TWS Connected';
    } else {
      dot.className = 'conn-dot connecting';
      label.textContent = 'TWS Connecting…';
    }
  } catch {
    const dot = document.getElementById('conn-dot');
    dot.className = 'conn-dot disconnected';
    document.getElementById('conn-label').textContent = 'Backend offline';
  }
}

async function refreshPortfolio() {
  const btn = document.getElementById('refresh-btn');
  btn.textContent = '⟳ Loading…';
  btn.disabled = true;

  try {
    const [portRes, cashRes] = await Promise.all([
      fetch(`${FLASK}/api/portfolio`),
      fetch(`${FLASK}/api/cash`),
    ]);
    portfolioData = await portRes.json();
    cashData = await cashRes.json();
  } catch (e) {
    showToast(`Refresh failed: ${e.message}`);
    portfolioData = portfolioData || { positions: [], account: {} };
    cashData = cashData || { settled_cash: 0, net_liquidation: 0 };
  }

  renderPortfolioTable();
  renderHeaderStats();
  renderMilestone();
  document.getElementById('positions-updated').textContent = `Updated ${new Date().toLocaleTimeString()}`;
  btn.textContent = '⟳ Refresh';
  btn.disabled = false;
}

function renderHeaderStats() {
  const acc = portfolioData.account || {};
  const netliq = parseFloat(acc.NetLiquidation || cashData.net_liquidation || 0);
  const cash = cashData.settled_cash || parseFloat(acc.AvailableFunds || 0);
  const positions = portfolioData.positions || [];
  const totalPnl = positions.reduce((s, p) => s + (p.unrealized_pnl || 0), 0);

  setText('stat-netliq', fmtCurrency(netliq));
  setText('stat-cash', fmtCurrency(cash));

  const pnlEl = document.getElementById('stat-pnl');
  pnlEl.textContent = (totalPnl >= 0 ? '+' : '') + fmtCurrency(totalPnl);
  pnlEl.style.color = totalPnl >= 0 ? 'var(--green)' : 'var(--red)';
}

function renderMilestone() {
  const acc = portfolioData.account || {};
  const netliq = parseFloat(acc.NetLiquidation || cashData.net_liquidation || 0);
  const milestones = [10000, 20000, 30000, 40000, 50000, 75000, 100000];
  let prev = 0, next = milestones[0];
  for (const m of milestones) {
    if (netliq < m) { next = m; break; }
    prev = m;
  }
  const pct = prev === next ? 100 : Math.min(100, ((netliq - prev) / (next - prev)) * 100);
  document.getElementById('milestone-bar').style.width = pct.toFixed(1) + '%';
  document.getElementById('milestone-label').textContent =
    netliq >= 100000 ? `$${(netliq/1000).toFixed(0)}k` : `$${fmtK(netliq)} → $${fmtK(next)}`;
}

function renderPortfolioTable() {
  const tbody = document.getElementById('positions-body');
  const positions = portfolioData.positions || [];

  if (!positions.length) {
    tbody.innerHTML = `<tr><td colspan="8" class="empty-row">
      ${portfolioData.error ? `⚠ ${portfolioData.error}` : 'No open positions — TWS may not be connected.'}
    </td></tr>`;
    return;
  }

  tbody.innerHTML = positions.map(p => {
    const gainCls = p.unrealized_pnl >= 0 ? 'gain' : 'loss';
    const sign = p.unrealized_pnl >= 0 ? '+' : '';
    const fracBadge = p.is_fractional ? '<span class="frac-badge">FRAC</span>' : '—';
    return `<tr>
      <td class="ticker-cell">${p.symbol}</td>
      <td class="num">${fmtQty(p.qty)}</td>
      <td class="num">$${p.avg_cost.toFixed(4)}</td>
      <td class="num">$${p.current_price.toFixed(4)}</td>
      <td class="num">$${p.market_value.toFixed(2)}</td>
      <td class="num ${gainCls}">${sign}$${p.unrealized_pnl.toFixed(2)}</td>
      <td class="num ${gainCls}">${sign}${p.unrealized_pnl_pct.toFixed(2)}%</td>
      <td class="num">${fracBadge}</td>
    </tr>`;
  }).join('');
}

async function analysePortfolio() {
  if (isStreaming) return;
  const positions = portfolioData.positions || [];
  if (!positions.length) { showToast('No positions to analyse.'); return; }

  const btn = document.getElementById('analyse-portfolio-btn');
  const output = document.getElementById('portfolio-ai-output');
  btn.disabled = true;
  btn.textContent = '⏳ Analysing…';
  output.innerHTML = '<p class="loading-dots streaming-cursor">Claude is searching for news and analysing your positions</p>';
  isStreaming = true;

  await streamAI('/api/ai/portfolio', { positions }, output, () => {
    btn.disabled = false;
    btn.textContent = '✦ Analyse All Positions';
    isStreaming = false;
  });
}

async function loadWishlist() {
  try {
    const res = await fetch(`${FLASK}/api/wishlist`);
    const items = await res.json();
    renderWishlistTable(items);
  } catch (e) {
    showToast('Failed to load wishlist');
  }
}

function renderWishlistTable(items) {
  const tbody = document.getElementById('wishlist-body');
  if (!items.length) {
    tbody.innerHTML = `<tr><td colspan="4" class="empty-row">No tickers — add some above.</td></tr>`;
    return;
  }
  tbody.innerHTML = items.map(w => `<tr>
    <td class="ticker-cell">${w.ticker}</td>
    <td>${w.added || ''}</td>
    <td>${w.notes || '—'}</td>
    <td><button class="delete-btn" onclick="removeWishlistTicker('${w.ticker}')">✕</button></td>
  </tr>`).join('');
}

async function addWishlistTicker() {
  const input = document.getElementById('wishlist-input');
  const ticker = input.value.trim().toUpperCase();
  if (!ticker) return;
  try {
    const res = await fetch(`${FLASK}/api/wishlist`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ ticker }),
    });
    const data = await res.json();
    if (data.error) { showToast(data.error); return; }
    input.value = '';
    loadWishlist();
  } catch (e) {
    showToast('Failed to add ticker');
  }
}

async function removeWishlistTicker(ticker) {
  try {
    await fetch(`${FLASK}/api/wishlist/${ticker}`, { method: 'DELETE' });
    loadWishlist();
  } catch (e) {
    showToast('Failed to remove ticker');
  }
}

async function analyseWishlist() {
  if (isStreaming) return;
  const btn = document.getElementById('analyse-wishlist-btn');
  const output = document.getElementById('wishlist-ai-output');

  try {
    const res = await fetch(`${FLASK}/api/wishlist`);
    const items = await res.json();
    if (!items.length) { showToast('Add some tickers first.'); return; }

    btn.disabled = true;
    btn.textContent = '⏳ Analysing…';
    output.innerHTML = '<p class="loading-dots streaming-cursor">Searching for data and ranking tickers…</p>';
    isStreaming = true;

    await streamAI('/api/ai/wishlist', { tickers: items }, output, () => {
      btn.disabled = false;
      btn.textContent = '✦ Rank by Priority';
      isStreaming = false;
    });
  } catch (e) {
    showToast('Failed to analyse wishlist');
  }
}

function populateNextMove() {
  const settled = cashData.settled_cash || 0;
  const netliq = cashData.net_liquidation || 0;
  setText('nm-settled-cash', fmtCurrency(settled));
  setText('nm-net-liq', fmtCurrency(netliq));
}

async function getNextMove() {
  if (isStreaming) return;
  const btn = document.getElementById('nextmove-btn');
  const output = document.getElementById('nextmove-ai-output');

  btn.disabled = true;
  btn.textContent = '⏳ Claude is thinking…';
  output.innerHTML = '<p class="loading-dots streaming-cursor">Searching for Stage 2 breakout candidates and calculating your deployment plan…</p>';
  isStreaming = true;

  const body = {
    settled_cash: cashData.settled_cash || 0,
    net_liquidation: cashData.net_liquidation || 0,
    positions: portfolioData.positions || [],
  };

  await streamAI('/api/ai/next-move', body, output, () => {
    btn.disabled = false;
    btn.textContent = "✦ Get Claude's Recommendation";
    isStreaming = false;
  });
}

function handleChatKey(e) {
  if (e.key === 'Enter' && !e.shiftKey) {
    e.preventDefault();
    sendChat();
  }
}

async function sendChat() {
  if (isStreaming) return;
  const input = document.getElementById('chat-input');
  const text = input.value.trim();
  if (!text) return;

  input.value = '';
  chatHistory.push({ role: 'user', content: text });
  appendChatMsg('user', text);

  const assistantDiv = appendChatMsg('assistant', '');
  const bubble = assistantDiv.querySelector('.chat-bubble');
  bubble.classList.add('streaming-cursor');

  const btn = document.getElementById('chat-send-btn');
  btn.disabled = true;
  isStreaming = true;

  let fullText = '';

  try {
    const response = await fetch(`${FLASK}/api/ai/chat`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        messages: chatHistory,
        positions: portfolioData.positions || [],
      }),
    });

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const d = JSON.parse(line.slice(6));
          if (d.text) {
            fullText += d.text;
            bubble.innerHTML = renderMarkdown(fullText);
          }
          if (d.error) fullText += `\n\n⚠ Error: ${d.error}`;
        } catch {}
      }
      scrollChatToBottom();
    }
  } catch (e) {
    fullText = `⚠ Connection error: ${e.message}`;
    bubble.textContent = fullText;
  }

  bubble.classList.remove('streaming-cursor');
  chatHistory.push({ role: 'assistant', content: fullText });
  isStreaming = false;
  btn.disabled = false;
  scrollChatToBottom();
}

function appendChatMsg(role, text) {
  const container = document.getElementById('chat-messages');
  const div = document.createElement('div');
  div.className = `chat-msg ${role}`;
  div.innerHTML = `<div class="chat-bubble">${role === 'user' ? escapeHtml(text) : renderMarkdown(text)}</div>`;
  container.appendChild(div);
  scrollChatToBottom();
  return div;
}

function scrollChatToBottom() {
  const c = document.getElementById('chat-messages');
  c.scrollTop = c.scrollHeight;
}

async function streamAI(path, body, outputEl, onDone) {
  let fullText = '';
  outputEl.innerHTML = '<div class="streaming-cursor" id="stream-target"></div>';

  try {
    const response = await fetch(`${FLASK}${path}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });

    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';

    while (true) {
      const { done, value } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split('\n');
      buffer = lines.pop();

      for (const line of lines) {
        if (!line.startsWith('data: ')) continue;
        try {
          const d = JSON.parse(line.slice(6));
          if (d.text) {
            fullText += d.text;
            outputEl.innerHTML = renderMarkdown(fullText);
            outputEl.scrollTop = outputEl.scrollHeight;
          }
          if (d.error) {
            fullText += `\n\n⚠ Error: ${d.error}`;
            outputEl.innerHTML = renderMarkdown(fullText);
          }
        } catch {}
      }
    }
  } catch (e) {
    outputEl.innerHTML = `<p style="color:var(--red)">⚠ ${e.message}</p>`;
  }

  if (onDone) onDone();
}

function renderMarkdown(text) {
  if (!text) return '';
  let html = escapeHtml(text);
  html = html.replace(/^### (.+)$/gm, '<h3>$1</h3>');
  html = html.replace(/^## (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/^# (.+)$/gm, '<h2>$1</h2>');
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>');
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>');
  html = html.replace(/`(.+?)`/g, '<code>$1</code>');
  html = html.replace(/^[-*] (.+)$/gm, '<li>$1</li>');
  html = html.replace(/(<li>.*<\/li>)/s, '<ul>$1</ul>');
  html = html.split(/\n{2,}/).map(block => {
    if (block.startsWith('<h') || block.startsWith('<ul') || block.startsWith('<li')) return block;
    return `<p>${block.replace(/\n/g, '<br>')}</p>`;
  }).join('');
  return html;
}

function fmtCurrency(n) {
  if (!n && n !== 0) return '—';
  return '$' + Number(n).toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function fmtK(n) {
  if (n >= 1000) return (n / 1000).toFixed(0) + 'k';
  return n.toString();
}

function fmtQty(n) {
  if (n === Math.floor(n)) return n.toString();
  return n.toFixed(4);
}

function setText(id, text) {
  const el = document.getElementById(id);
  if (el) el.textContent = text;
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function showToast(msg) {
  const toast = document.createElement('div');
  toast.className = 'toast';
  toast.textContent = msg;
  document.body.appendChild(toast);
  setTimeout(() => toast.remove(), 4000);
}
