// 公共前端工具：请求封装、提示条、表单收集、轮询。

function toast(msg, bad) {
  const el = document.getElementById('toast');
  if (!el) { alert(msg); return; }
  el.textContent = msg;
  el.className = 'toast show' + (bad ? ' bad' : '');
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = 'toast'; }, 2600);
}

async function api(url, opts) {
  opts = opts || {};
  const init = { method: opts.method || 'GET', headers: {} };
  if (opts.body !== undefined) {
    if (opts.body instanceof FormData) {
      init.body = opts.body;
    } else {
      init.headers['Content-Type'] = 'application/json';
      init.body = JSON.stringify(opts.body);
    }
  }
  let r, data;
  try {
    r = await fetch(url, init);
  } catch (e) {
    toast('网络错误: ' + e.message, true);
    throw e;
  }
  try { data = await r.json(); } catch (e) { data = { ok: r.ok, error: '响应不是 JSON' }; }
  if (!r.ok || data.ok === false) {
    toast(data.error || ('请求失败 HTTP ' + r.status), true);
    throw new Error(data.error || r.status);
  }
  return data;
}

// 收集容器内所有 [name] 控件的值
function collect(root) {
  const out = {};
  root.querySelectorAll('[name]').forEach(el => {
    if (el.type === 'checkbox') out[el.name] = el.checked ? 1 : 0;
    else if (el.multiple) out[el.name] = Array.from(el.selectedOptions).map(o => o.value);
    else out[el.name] = el.value;
  });
  return out;
}

function fmtBytesMb(mb) {
  return mb > 1024 ? (mb / 1024).toFixed(1) + ' GB' : mb.toFixed(1) + ' MB';
}

function barClass(pct) { return pct >= 90 ? 'bad' : pct >= 70 ? 'warn' : ''; }

// 定时刷新：cb 返回 Promise，页面隐藏时暂停
function poll(cb, ms) {
  let stop = false;
  (async function loop() {
    while (!stop) {
      if (!document.hidden) { try { await cb(); } catch (e) { /* 忽略单次失败 */ } }
      await new Promise(r => setTimeout(r, ms));
    }
  })();
  return () => { stop = true; };
}

function confirmDo(msg, fn) { if (window.confirm(msg)) fn(); }
