from __future__ import annotations

import json
import fcntl
import socket
import os
import secrets
import signal
import subprocess
import sys
import stat
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from .config import LibraryConfig
from .database import Database
from .utils import MediaManagerError


INDEX_HTML = r"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>media-manager</title><style>
:root{color-scheme:dark;--page:#151a22;--shell:#090d14;--top:rgba(6,9,14,.96);--surface:#111824;--surface2:#172131;--hover:#202d40;--line:rgba(137,157,190,.16);--line2:rgba(137,157,190,.28);--text:#edf4fb;--muted:#91a2b8;--faint:#607187;--accent:#4de3c1;--accent2:#6aa8ff;--accent-soft:rgba(77,227,193,.12);--ok:#57e39b;--warn:#f4cf6a;--bad:#ff7b85;--radius:9px;--sidebar:248px;--topbar:64px;font-family:Inter,ui-sans-serif,system-ui,-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;color:var(--text);background:var(--page);font-synthesis:none;-webkit-font-smoothing:antialiased}*{box-sizing:border-box}[hidden]{display:none!important}body{margin:0;min-width:320px;min-height:100vh;background-color:var(--page);background-image:radial-gradient(circle,rgba(214,222,232,.15) 0 .6px,transparent .8px);background-size:37px 41px}button,input,select{font:inherit}button:focus-visible,input:focus-visible,select:focus-visible,[tabindex]:focus-visible{outline:2px solid var(--accent);outline-offset:2px}h1,h2,h3,p{margin-top:0}h1{margin-bottom:5px;font-size:25px;letter-spacing:-.02em}h2{margin-bottom:5px;font-size:16px}h3{margin:22px 0 4px;font-size:13px}.muted{color:var(--muted)}.error{color:var(--bad)}.eyebrow{margin:0 0 7px;color:var(--accent);font-size:10px;font-weight:800;letter-spacing:.16em;text-transform:uppercase}.button{display:inline-flex;align-items:center;justify-content:center;gap:7px;min-height:36px;border:1px solid var(--line2);border-radius:7px;padding:7px 13px;color:var(--text);background:var(--surface2);cursor:pointer;font-size:12px;font-weight:700;transition:.15s ease}.button:hover{border-color:var(--accent2);background:var(--hover)}.button:active{transform:translateY(1px)}.button:disabled{opacity:.4;cursor:not-allowed}.button.primary{border-color:transparent;color:#06251f;background:linear-gradient(135deg,var(--accent),var(--accent2));box-shadow:0 8px 22px -13px var(--accent)}.button.danger{border-color:rgba(255,123,133,.3);color:var(--bad);background:rgba(255,123,133,.09)}.button.quiet{border-color:transparent;background:transparent;color:var(--muted)}.button.full{width:100%}.icon-button{display:grid;width:38px;height:38px;place-items:center;border:1px solid var(--line2);border-radius:7px;padding:0;color:var(--muted);background:var(--surface2);cursor:pointer;font-size:19px}.icon-button:hover{color:var(--text);border-color:var(--accent2);background:var(--hover)}input,select{width:100%;min-width:0;border:1px solid var(--line2);border-radius:7px;padding:9px 11px;color:var(--text);background:#090e17}input:focus,select:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-soft);outline:0}label{display:block;margin:13px 0 5px;color:var(--muted);font-size:12px;font-weight:650}.auth-shell{display:grid;min-height:100vh;place-items:center;padding:24px}.auth-card{width:min(100%,460px);border:1px solid var(--line);border-radius:18px;padding:38px;background:rgba(17,24,36,.92);box-shadow:0 30px 70px -32px #000;backdrop-filter:blur(18px)}.auth-card.setup-card{width:min(100%,820px)}.auth-brand{display:flex;align-items:center;gap:13px;margin-bottom:27px}.logo{display:grid;width:44px;height:44px;place-items:center;border-radius:11px;color:#06251f;background:linear-gradient(135deg,var(--accent),var(--accent2));font-weight:900;letter-spacing:-.08em;box-shadow:0 12px 30px -15px var(--accent)}.auth-brand strong{display:block;font-size:16px}.auth-brand small{color:var(--muted)}.auth-actions{display:flex;align-items:center;gap:12px;margin-top:24px}.topbar{position:fixed;z-index:30;inset:0 0 auto 0;height:var(--topbar);display:flex;align-items:center;justify-content:space-between;border-bottom:1px solid var(--line);padding:0 20px;background:var(--top);backdrop-filter:blur(16px)}.topbar-left,.top-actions,.brand{display:flex;align-items:center}.topbar-left{gap:12px}.brand{gap:11px}.brand .logo{width:36px;height:36px;border-radius:9px;font-size:13px}.brand strong,.brand small{display:block}.brand strong{font-size:14px}.brand small{margin-top:1px;color:var(--muted);font-size:10px}.top-actions{gap:8px}.service-state{display:flex;align-items:center;gap:7px;margin-right:8px;color:var(--muted);font-size:11px;font-weight:700}.service-state:before{content:"";width:7px;height:7px;border-radius:50%;background:var(--ok);box-shadow:0 0 0 3px rgba(87,227,155,.1)}.sidebar{position:fixed;z-index:20;inset:var(--topbar) auto 0 0;width:var(--sidebar);display:flex;flex-direction:column;border-right:1px solid var(--line);padding:20px 14px;background:rgba(9,13,20,.94);transition:transform .2s ease}.sidebar-heading{padding:0 10px 13px}.sidebar-heading h2{font-size:18px}.nav{display:grid;gap:5px}.nav-button{display:grid;grid-template-columns:28px 1fr auto;align-items:center;gap:9px;width:100%;border:1px solid transparent;border-radius:8px;padding:9px 10px;color:var(--muted);background:transparent;cursor:pointer;text-align:left}.nav-button:hover{color:var(--text);background:var(--hover)}.nav-button.selected{border-color:var(--line);color:var(--text);background:var(--accent-soft)}.nav-icon{display:grid;width:27px;height:27px;place-items:center;border-radius:7px;color:var(--accent);background:rgba(77,227,193,.08);font-size:15px}.nav-button strong{display:block;font-size:12px}.nav-button small{display:block;margin-top:2px;color:var(--faint);font-size:10px}.count-badge{min-width:22px;border-radius:99px;padding:2px 6px;color:var(--muted);background:var(--surface2);font-size:10px;font-weight:800;text-align:center}.sidebar-footer{margin-top:auto;border-top:1px solid var(--line);padding:15px 9px 0}.library-path{margin:4px 0 14px;overflow:hidden;color:var(--muted);font-size:10px;text-overflow:ellipsis;white-space:nowrap}.sidebar-footer .button{justify-content:flex-start;width:100%}.workspace{min-height:100vh;margin-left:var(--sidebar);padding:calc(var(--topbar) + 31px) clamp(20px,3vw,44px) 44px;transition:margin .2s ease}.page{max-width:1260px;margin:0 auto}.page-header{display:flex;align-items:flex-start;justify-content:space-between;gap:24px;margin-bottom:24px}.page-header p{margin-bottom:0}.header-actions{display:flex;gap:8px}.panel{border:1px solid var(--line);border-radius:var(--radius);background:rgba(17,24,36,.84);box-shadow:0 14px 35px -28px #000}.panel-header{display:flex;align-items:center;justify-content:space-between;gap:12px;border-bottom:1px solid var(--line);padding:15px 17px}.panel-header h2{margin:0}.panel-body{padding:17px}.control-band{display:grid;grid-template-columns:1fr auto;gap:18px;margin-bottom:18px;padding:16px}.run-options{display:grid;grid-template-columns:minmax(110px,160px) minmax(160px,240px);gap:9px}.run-actions{display:flex;align-items:center;gap:8px}.current-grid{display:grid;grid-template-columns:minmax(0,1.65fr) minmax(260px,.75fr);gap:18px;margin-bottom:18px}.run-card{position:relative;min-height:184px;overflow:hidden}.run-card:before{content:"";position:absolute;inset:0 auto 0 0;width:3px;background:var(--accent)}.run-summary{display:flex;align-items:center;gap:12px;margin-bottom:16px}.run-state-icon{display:grid;width:40px;height:40px;place-items:center;border-radius:10px;color:var(--accent);background:var(--accent-soft);font-size:18px}.run-summary strong{display:block}.run-summary small{color:var(--muted)}.stage-box{border:1px solid var(--line);border-radius:7px;padding:13px;background:#0a1019}.stage-top{display:flex;align-items:center;justify-content:space-between;gap:12px}.stage-path{overflow:hidden;font-size:12px;font-weight:700;text-overflow:ellipsis;white-space:nowrap}.stage-name{margin-top:6px;color:var(--muted);font-size:11px}.progress-track{height:5px;margin-top:13px;overflow:hidden;border-radius:5px;background:#202b3d}.progress-track span{display:block;width:35%;height:100%;border-radius:inherit;background:linear-gradient(90deg,var(--accent),var(--accent2));animation:pulse 1.5s ease-in-out infinite alternate}@keyframes pulse{to{opacity:.5;transform:translateX(180%)}}.idle-box{display:grid;min-height:83px;place-items:center;border:1px dashed var(--line2);border-radius:7px;color:var(--muted);font-size:12px}.stats{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}.stat{min-height:87px;border:1px solid var(--line);border-radius:8px;padding:13px;background:rgba(9,14,23,.55)}.stat span{display:block;color:var(--muted);font-size:10px;text-transform:capitalize}.stat b{display:block;margin-top:8px;font-size:22px;font-variant-numeric:tabular-nums}.stat.ok b{color:var(--ok)}.stat.warn b{color:var(--warn)}.stat.bad b{color:var(--bad)}.content-grid{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(320px,.65fr);gap:18px}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:11px 14px;border-bottom:1px solid var(--line);vertical-align:middle}th{color:var(--faint);font-size:9px;letter-spacing:.11em;text-transform:uppercase}td{font-size:11px}tbody tr:last-child td{border-bottom:0}tr.click{cursor:pointer}tr.click:hover{background:var(--hover)}.path-cell{max-width:360px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-weight:650}.reason-cell{max-width:360px;color:var(--muted)}.pill{display:inline-flex;border:1px solid var(--line2);border-radius:99px;padding:3px 7px;color:var(--muted);background:var(--surface2);font-size:9px;font-weight:800;text-transform:uppercase;white-space:nowrap}.pill.managed,.pill.complete{color:var(--ok);background:rgba(87,227,155,.08)}.pill.failed{color:var(--bad);background:rgba(255,123,133,.08)}.pill.needs_review,.pill.insufficient_evidence,.pill.managed_with_gaps{color:var(--warn);background:rgba(244,207,106,.08)}.empty{display:grid;min-height:160px;place-items:center;padding:28px;color:var(--muted);font-size:12px;text-align:center}.view-all{border:0;color:var(--accent);background:transparent;cursor:pointer;font-size:11px;font-weight:700}.formgrid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:0 16px}.check-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:9px;margin-top:18px}.check{display:flex;align-items:center;gap:8px;margin:0;border:1px solid var(--line);border-radius:7px;padding:10px;background:#0a1019}.check input{width:auto;margin:0}.wide{width:100%}.notice{position:fixed;z-index:100;right:20px;bottom:20px;max-width:420px;border:1px solid var(--line2);border-radius:9px;padding:12px 15px;color:var(--text);background:#111a28;box-shadow:0 16px 40px #0008;font-size:12px}.notice.bad{border-color:rgba(255,123,133,.35);color:var(--bad)}dialog{width:min(820px,calc(100vw - 32px));max-height:88vh;overflow:auto;border:1px solid var(--line2);border-radius:13px;padding:0;color:var(--text);background:var(--surface);box-shadow:0 30px 80px #000b}dialog::backdrop{background:#02050acc;backdrop-filter:blur(3px)}.dialog-head{position:sticky;z-index:2;top:0;display:flex;align-items:flex-start;justify-content:space-between;gap:20px;border-bottom:1px solid var(--line);padding:18px 20px;background:var(--surface)}.dialog-head h2{margin:0}.dialog-body{padding:20px}.dialog-actions{display:flex;align-items:center;gap:12px;margin-top:22px}pre{max-height:520px;overflow:auto;white-space:pre-wrap;overflow-wrap:anywhere;border:1px solid var(--line);border-radius:8px;padding:14px;color:#c7d6e8;background:#080d15;font:11px/1.55 ui-monospace,SFMono-Regular,Consolas,monospace}body.sidebar-closed .sidebar{transform:translateX(-100%)}body.sidebar-closed .workspace{margin-left:0}@media(max-width:900px){.sidebar{box-shadow:20px 0 50px #0008}.workspace{margin-left:0}.sidebar:not(.open){transform:translateX(-100%)}.current-grid,.content-grid{grid-template-columns:1fr}.top-actions .service-state{display:none}}@media(max-width:620px){.topbar{padding:0 12px}.brand small,.top-actions .quiet{display:none}.workspace{padding:calc(var(--topbar) + 22px) 14px 30px}.page-header{display:block}.header-actions{margin-top:15px}.control-band{grid-template-columns:1fr}.run-options,.formgrid,.check-grid{grid-template-columns:1fr}.run-actions{flex-wrap:wrap}.auth-card{padding:28px 22px}.stats{grid-template-columns:1fr 1fr}.path-cell{max-width:180px}.reason-cell{display:none}}
.panel,.current-grid>*,.content-grid>*{min-width:0;max-width:100%}.panel{overflow:hidden}.panel-body,#reviews,#runs,#reviewsPreview,#runsPreview{min-width:0;max-width:100%}.data-table{width:100%;table-layout:fixed}.data-table th,.data-table td{min-width:0;overflow-wrap:anywhere;word-break:break-word}.review-table th:nth-child(1){width:34%}.review-table th:nth-child(2){width:16%}.review-table th:nth-child(3){width:18%}.review-table th:nth-child(4){width:32%}.run-table th:nth-child(1),.run-table th:nth-child(3){width:25%}.run-table th:nth-child(2){width:16%}.run-table th:nth-child(4){width:34%}.run-table td{font-variant-numeric:tabular-nums}.compact-table thead{display:none}.compact-table,.compact-table tbody,.compact-table tr{display:block;width:100%}.compact-table tr{padding:11px 14px;border-bottom:1px solid var(--line)}.compact-table tr:last-child{border-bottom:0}.compact-table td{display:grid;grid-template-columns:58px minmax(0,1fr);gap:8px;border:0;padding:3px 0}.compact-table td:before{content:attr(data-label);color:var(--faint);font-size:8px;font-weight:800;letter-spacing:.1em;text-transform:uppercase}.compact-table .reason-cell{display:grid!important;max-width:none}.pill.not_needed,.pill.not_applicable{color:var(--ok);background:rgba(87,227,155,.08)}.pill.unavailable{color:var(--warn);background:rgba(244,207,106,.08)}.detail-summary{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:10px;margin-bottom:20px}.detail-stat{border:1px solid var(--line);border-radius:8px;padding:13px;background:#0a1019}.detail-stat span{display:block;color:var(--muted);font-size:10px;text-transform:uppercase}.detail-stat b{display:block;margin-top:6px;font-size:21px}.detail-stat.ok b{color:var(--ok)}.detail-stat.bad b{color:var(--bad)}.media-meta{display:flex;align-items:center;gap:8px;margin-bottom:18px;color:var(--muted);font-size:11px}.stage-list{display:grid;gap:8px}.stage-row{border:1px solid var(--line);border-radius:8px;padding:12px 13px;background:#0a1019}.stage-row-head{display:flex;align-items:center;justify-content:space-between;gap:12px}.stage-row strong{font-size:12px}.stage-reason{margin:7px 0 0;color:var(--muted);font-size:11px;overflow-wrap:anywhere}.stage-row details{margin-top:9px}.stage-row summary{color:var(--faint);cursor:pointer;font-size:10px}.event-list{display:grid;gap:7px;margin-top:10px}.event-row{display:grid;grid-template-columns:132px minmax(0,1fr);gap:10px;border-bottom:1px solid var(--line);padding:8px 0;color:var(--muted);font-size:10px}.event-row:last-child{border-bottom:0}.detail-run{display:flex;align-items:center;justify-content:space-between;gap:18px;margin-bottom:20px;border:1px solid var(--line);border-radius:8px;padding:13px;background:var(--accent-soft)}.detail-run p{margin:3px 0 0;color:var(--muted);font-size:10px}@media(max-width:700px){.data-table:not(.compact-table),.data-table:not(.compact-table) tbody,.data-table:not(.compact-table) tr{display:block;width:100%}.data-table:not(.compact-table) thead{display:none}.data-table:not(.compact-table) tr{padding:11px 14px;border-bottom:1px solid var(--line)}.data-table:not(.compact-table) tr:last-child{border-bottom:0}.data-table:not(.compact-table) td{display:grid!important;grid-template-columns:76px minmax(0,1fr);gap:9px;max-width:none;border:0;padding:4px 0;white-space:normal}.data-table:not(.compact-table) td:before{content:attr(data-label);color:var(--faint);font-size:8px;font-weight:800;letter-spacing:.1em;text-transform:uppercase}.detail-summary{grid-template-columns:1fr 1fr}.detail-run{align-items:flex-start;flex-direction:column}.event-row{grid-template-columns:1fr}}
</style></head><body>
<section id="setup" class="auth-shell" hidden><div class="auth-card setup-card"><div class="auth-brand"><span class="logo">MM</span><span><strong>media-manager</strong><small>First-time configuration</small></span></div><p class="eyebrow">WELCOME</p><h1>Set up your library</h1><p class="muted">Create administrator access and choose how media should be processed.</p><div id="setupFields"></div><div class="auth-actions"><button class="button primary" onclick="submitSetup()">Save and open dashboard</button><span id="setupmsg" class="muted"></span></div></div></section>
<section id="login" class="auth-shell"><div class="auth-card"><div class="auth-brand"><span class="logo">MM</span><span><strong>media-manager</strong><small>Library operations console</small></span></div><p class="eyebrow">PRIVATE DASHBOARD</p><h1>Welcome back</h1><p class="muted">Enter the dashboard token you created during setup.</p><label for="token">Dashboard token</label><input id="token" type="password" placeholder="Enter token" autocomplete="current-password" onkeydown="if(event.key==='Enter')connect()"><div class="auth-actions"><button class="button primary full" onclick="connect()">Sign in</button></div><p id="authmsg" class="error" role="alert"></p></div></section>
<div id="app" hidden><header class="topbar"><div class="topbar-left"><button class="icon-button" onclick="toggleSidebar()" aria-label="Toggle sidebar">☰</button><div class="brand"><span class="logo">MM</span><span><strong>media-manager</strong><small>Stateful media operations</small></span></div></div><div class="top-actions"><span class="service-state">Service online</span><button class="button quiet" onclick="refresh()">Refresh</button><button class="button quiet" onclick="logout()">Sign out</button></div></header>
<aside id="sidebar" class="sidebar"><div class="sidebar-heading"><p class="eyebrow">LIBRARY</p><h2>Operations</h2></div><nav class="nav"><button class="nav-button selected" data-view="overview" onclick="showView('overview')"><span class="nav-icon">⌂</span><span><strong>Overview</strong><small>Health and controls</small></span></button><button class="nav-button" data-view="reviews" onclick="showView('reviews')"><span class="nav-icon">!</span><span><strong>Review queue</strong><small>Items needing attention</small></span><span id="reviewBadge" class="count-badge">0</span></button><button class="nav-button" data-view="history" onclick="showView('history')"><span class="nav-icon">↻</span><span><strong>Run history</strong><small>Recent activity</small></span></button></nav><div class="sidebar-footer"><p class="eyebrow">MOUNTED LIBRARY</p><div id="library" class="library-path" title="">Loading…</div><button class="button quiet" onclick="openSettings()">⚙ &nbsp;Settings</button></div></aside>
<main class="workspace"><section id="overviewView" class="page"><header class="page-header"><div><p class="eyebrow">LIBRARY OVERVIEW</p><h1>Media operations</h1><p class="muted">Monitor processing, retry reviewed items, and inspect pipeline decisions.</p></div><div class="header-actions"><button class="button" onclick="showView('reviews')">Open review queue</button></div></header>
<section class="panel control-band"><div class="run-options"><input id="limit" type="number" min="1" placeholder="Optional item limit"><input id="stage" placeholder="Review stage filter"></div><div class="run-actions"><button class="button primary" id="startReview" onclick="startRun('reviewed')">▶ Run reviewed</button><button class="button" id="startFull" onclick="startRun('full')">Scan + run pending</button><button class="button danger" id="pause" onclick="pauseRun()">Ⅱ Pause safely</button></div></section>
<div class="current-grid"><section class="panel run-card"><div class="panel-header"><div><p class="eyebrow">PROCESSING</p><h2>Current run</h2></div><span id="runPill" class="pill">IDLE</span></div><div class="panel-body"><div id="currentRun"></div></div></section><section class="panel"><div class="panel-header"><div><p class="eyebrow">LIBRARY STATE</p><h2>At a glance</h2></div></div><div class="panel-body"><div class="stats" id="stats"></div></div></section></div>
<div class="content-grid"><section class="panel"><div class="panel-header"><div><p class="eyebrow">ATTENTION</p><h2>Review queue</h2></div><button class="view-all" onclick="showView('reviews')">View all →</button></div><div id="reviewsPreview"></div></section><section class="panel"><div class="panel-header"><div><p class="eyebrow">ACTIVITY</p><h2>Recent runs</h2></div><button class="view-all" onclick="showView('history')">View all →</button></div><div id="runsPreview"></div></section></div></section>
<section id="reviewsView" class="page" hidden><header class="page-header"><div><p class="eyebrow">ACTION REQUIRED</p><h1>Review queue</h1><p class="muted">Open an item to inspect stage evidence and recorded decisions.</p></div><button class="button primary" id="reviewPageRun" onclick="startRun('reviewed')">▶ Retry reviewed items</button></header><section class="panel"><div class="panel-header"><h2>Items requiring attention</h2><span id="reviewTotal" class="count-badge">0</span></div><div id="reviews"></div></section></section>
<section id="historyView" class="page" hidden><header class="page-header"><div><p class="eyebrow">AUDIT TRAIL</p><h1>Run history</h1><p class="muted">Recent processing sessions and their completion state.</p></div></header><section class="panel"><div class="panel-header"><h2>Recent sessions</h2></div><div id="runs"></div></section></section></main></div>
<div id="notice" class="notice" hidden></div>
<dialog id="detail"><div class="dialog-head"><div><p class="eyebrow">MEDIA DETAILS</p><h2 id="detailTitle"></h2></div><button class="icon-button" onclick="document.getElementById('detail').close()" aria-label="Close">×</button></div><div class="dialog-body"><div id="detailBody"></div></div></dialog>
<dialog id="settings"><div class="dialog-head"><div><p class="eyebrow">CONFIGURATION</p><h2>Settings</h2></div><button class="icon-button" onclick="document.getElementById('settings').close()" aria-label="Close">×</button></div><div class="dialog-body"><p class="muted">Pause active processing before changing settings. Leave secrets blank to keep their current values.</p><div id="settingsFields"></div><div class="dialog-actions"><button class="button primary" onclick="saveSettings()">Save settings</button><span id="settingsmsg" class="muted"></span></div></div></dialog>
<script>
let auth='',noticeTimer,dashboardBusy=false;const $=id=>document.getElementById(id);
function esc(s){return String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
function label(s){return String(s||'unknown').replaceAll('_',' ')}
function safeClass(s){return ['managed','complete','failed','needs_review','insufficient_evidence','managed_with_gaps','not_needed','not_applicable','unavailable','pending','running'].includes(s)?s:''}
function notify(message,bad=false){const n=$('notice');n.textContent=message;n.className='notice'+(bad?' bad':'');n.hidden=false;clearTimeout(noticeTimer);noticeTimer=setTimeout(()=>n.hidden=true,5000)}
async function api(path,opt={},authorized=true){opt.headers={...(opt.headers||{})};if(authorized)opt.headers.Authorization='Bearer '+auth;if(opt.body)opt.headers['Content-Type']='application/json';const r=await fetch(path,opt);let d={};try{d=await r.json()}catch{}if(!r.ok)throw Error(d.error||r.statusText);return d}
function formFields(v={},setup=false){return `<div class="formgrid">${setup?`<div><label>Dashboard token · 16+ characters</label><input data-key="dashboard_token" type="password" autocomplete="new-password" placeholder="Create a secure token"></div>`:''}<div><label>Movies folder inside library</label><input data-key="movies_directory" value="${esc(v.movies_directory||'Movies')}"></div><div><label>TV folder inside library</label><input data-key="tv_directory" value="${esc(v.tv_directory||'TV Shows')}"></div><div><label>Subtitle languages · comma-separated</label><input data-key="subtitle_languages" value="${esc((v.subtitle_languages||['en']).join(','))}"></div><div><label>SyncNet device</label><select data-key="syncnet_device"><option value="cpu" ${v.syncnet_device!=='cuda'?'selected':''}>CPU</option><option value="cuda" ${v.syncnet_device==='cuda'?'selected':''}>CUDA GPU</option></select></div></div><div class="check-grid"><label class="check"><input data-key="av_sync_enabled" type="checkbox" ${v.av_sync_enabled!==false?'checked':''}> A/V synchronization</label><label class="check"><input data-key="crop_enabled" type="checkbox" ${v.crop_enabled!==false?'checked':''}> Black-bar crop</label><label class="check"><input data-key="subtitles_enabled" type="checkbox" ${v.subtitles_enabled!==false?'checked':''}> Subtitle synchronization</label></div><h3>OpenSubtitles <span class="muted">· optional</span></h3><div class="formgrid"><div><label>Username</label><input data-key="opensubtitles_username" value="${esc(v.opensubtitles_username||'')}"></div><div><label>API key ${v.opensubtitles_api_key_configured?'· configured':''}</label><input data-key="opensubtitles_api_key" type="password" autocomplete="off"></div><div><label>Password ${v.opensubtitles_password_configured?'· configured':''}</label><input data-key="opensubtitles_password" type="password" autocomplete="off"></div>${setup?'':`<div><label>New dashboard token · optional</label><input data-key="dashboard_token" type="password" autocomplete="new-password"></div>`}</div>`}
function formData(parent){const d={};parent.querySelectorAll('[data-key]').forEach(e=>d[e.dataset.key]=e.type==='checkbox'?e.checked:e.value.trim());return d}
function toggleSidebar(){if(innerWidth<=900)$('sidebar').classList.toggle('open');else document.body.classList.toggle('sidebar-closed')}
function showView(name){['overview','reviews','history'].forEach(v=>$(v+'View').hidden=v!==name);document.querySelectorAll('.nav-button').forEach(b=>b.classList.toggle('selected',b.dataset.view===name));if(innerWidth<=900)$('sidebar').classList.remove('open');scrollTo({top:0,behavior:'smooth'})}
function logout(){sessionStorage.removeItem('mediaManagerToken');auth='';location.reload()}
function reviewTable(rows){if(!rows.length)return '<div class="empty">Everything is clear.<br>No media currently requires review.</div>';return `<table class="data-table review-table"><thead><tr><th>Media</th><th>Stage</th><th>Status</th><th>Reason</th></tr></thead><tbody>${rows.map(r=>`<tr class="click" onclick="showMedia(${r.media_id})"><td data-label="Media" class="path-cell" title="${esc(r.path)}">${esc(r.path)}</td><td data-label="Stage">${esc(label(r.stage))}</td><td data-label="Status"><span class="pill ${safeClass(r.status)}">${esc(label(r.status))}</span></td><td data-label="Reason" class="reason-cell">${esc(r.reason||'Open for details')}</td></tr>`).join('')}</tbody></table>`}
function runTable(rows,compact=false){if(!rows.length)return '<div class="empty">No processing sessions have been recorded yet.</div>';return `<table class="data-table run-table${compact?' compact-table':''}"><thead><tr><th>Started</th><th>Status</th><th>Ended</th><th>Reason</th></tr></thead><tbody>${rows.map(r=>`<tr><td data-label="Started">${esc(r.started_at||'—')}</td><td data-label="Status"><span class="pill ${safeClass(r.status)}">${esc(label(r.status))}</span></td><td data-label="Ended">${esc(r.ended_at||'In progress')}</td><td data-label="Reason" class="reason-cell">${esc(r.reason||'—')}</td></tr>`).join('')}</tbody></table>`}
function renderStats(counts){const cards=[['managed','Managed','ok'],['managed_with_gaps','With gaps','warn'],['needs_review','Review','warn'],['failed','Failed','bad']];$('stats').innerHTML=cards.map(([key,name,tone])=>`<div class="stat ${tone}"><span>${name}</span><b>${Number(counts[key]||0)}</b></div>`).join('')}
function renderCurrent(d){const active=d.active_run,stage=d.current_stage;if(!active){$('runPill').className='pill';$('runPill').textContent='IDLE';$('currentRun').innerHTML='<div class="idle-box">No processing run is active.</div>';return}$('runPill').className='pill complete';$('runPill').textContent='RUNNING';const stageHtml=stage?`<div class="stage-box"><div class="stage-top"><span class="stage-path" title="${esc(stage.path)}">${esc(stage.path)}</span><span class="pill">${esc(label(stage.stage))}</span></div><div class="stage-name">${esc(label(stage.phase||'Working'))} · updated ${esc(stage.updated_at||'now')}</div><div class="progress-track"><span></span></div><details><summary class="muted">Technical details</summary><pre>${esc(JSON.stringify(stage.details,null,2))}</pre></details></div>`:'<div class="idle-box">Waiting for the next pipeline stage…</div>';$('currentRun').innerHTML=`<div class="run-summary"><span class="run-state-icon">▶</span><span><strong>Processing is active</strong><small>PID ${esc(active.pid)} · started ${esc(active.started_at)}</small></span></div>${stageHtml}`}
function stageReason(stage){const d=stage.details||{};let reason=d.reason||d.error||d.correction_error||d.message||'';if(stage.status==='waiting')reason='Waiting for suitable subtitles. Next eligible retry: '+(d.next_eligible_attempt||'unknown')+'. '+reason;if(d.diagnostics?.drift_verified===false)reason+='; drift unverified';if(d.selection)reason+='; '+d.selection.reference_reason;return reason}
function renderMediaDetail(d){const entries=Object.entries(d.stages||{}),passed=new Set(['complete','not_needed','not_applicable']),retryable=new Set(['failed','needs_review','insufficient_evidence']);let passCount=0,attentionCount=0;for(const [,s] of entries){if(passed.has(s.status))passCount++;else if(retryable.has(s.status)||s.status==='unavailable')attentionCount++}const otherCount=entries.length-passCount-attentionCount;const canRetry=entries.some(([,s])=>retryable.has(s.status));const stages=entries.length?entries.map(([name,s])=>{const reason=stageReason(s);return `<article class="stage-row"><div class="stage-row-head"><strong>${esc(label(name))}</strong><span class="pill ${safeClass(s.status)}">${esc(label(s.status))}</span></div>${reason?`<p class="stage-reason">${esc(reason)}</p>`:''}<p class="stage-reason">Updated ${esc(s.updated_at||'—')}</p><details><summary>Show recorded evidence</summary><pre>${esc(JSON.stringify(s.details||{},null,2))}</pre></details></article>`}).join(''):'<div class="empty">No pipeline stages have been recorded for this item.</div>';const events=(d.events||[]).slice(0,20).map(e=>`<div class="event-row"><span>${esc(e.created_at||'—')} · ${esc(label(e.kind))}</span><span>${esc(e.message||'')}</span></div>`).join('')||'<p class="muted">No events recorded.</p>';return `<div class="media-meta"><span class="pill ${safeClass(d.media.overall_status)}">${esc(label(d.media.overall_status))}</span><span>${esc(label(d.media.media_type))}</span><span>${d.media.duration?Math.round(Number(d.media.duration)/60)+' min':'Duration unavailable'}</span></div><div class="detail-summary"><div class="detail-stat ok"><span>Passed</span><b>${passCount}</b></div><div class="detail-stat bad"><span>Needs attention</span><b>${attentionCount}</b></div><div class="detail-stat"><span>Other</span><b>${otherCount}</b></div></div><div class="detail-run"><div><strong>Retry only this media item</strong><p>Successful stages stay preserved; only unsuccessful stages and required verification run again.</p></div><button class="button primary" onclick="runMedia(${Number(d.media.id)})" ${!canRetry||dashboardBusy?'disabled':''}>▶ Run only this item</button></div><p class="eyebrow">PIPELINE STAGES</p><div class="stage-list">${stages}</div><h3>Recent events</h3><div class="event-list">${events}</div>`}
async function bootstrap(){const b=await api('/api/bootstrap',{},false);$('library').textContent=b.library;$('library').title=b.library;if(b.setup_required){$('setupFields').innerHTML=formFields(b.defaults,true);$('setup').hidden=false;$('login').hidden=true}else{$('setup').hidden=true;$('login').hidden=false;auth=sessionStorage.getItem('mediaManagerToken')||'';$('token').value=auth;if(auth)connect()}}
async function submitSetup(){const msg=$('setupmsg');try{msg.textContent='Saving…';const d=formData($('setupFields'));await api('/api/setup',{method:'POST',body:JSON.stringify(d)},false);auth=d.dashboard_token;sessionStorage.setItem('mediaManagerToken',auth);$('setup').hidden=true;await connect();notify('Setup complete')}catch(e){msg.innerHTML='<span class="error">'+esc(e.message)+'</span>'}}
async function connect(){auth=$('token').value.trim()||auth;if(!auth){$('authmsg').textContent='Enter your dashboard token.';return}try{await refresh();sessionStorage.setItem('mediaManagerToken',auth);$('app').hidden=false;$('login').hidden=true;$('authmsg').textContent=''}catch(e){$('login').hidden=false;$('authmsg').textContent=e.message}}
async function refresh(){const d=await api('/api/status');$('library').textContent=d.library;$('library').title=d.library;renderStats(d.counts);renderCurrent(d);$('reviews').innerHTML=reviewTable(d.reviews);$('reviewsPreview').innerHTML=reviewTable(d.reviews.slice(0,5));$('runs').innerHTML=runTable(d.runs);$('runsPreview').innerHTML=runTable(d.runs.slice(0,5),true);$('reviewBadge').textContent=d.reviews.length;$('reviewTotal').textContent=d.reviews.length;dashboardBusy=!!d.active_run;$('pause').disabled=!dashboardBusy||!d.active_run.can_pause;$('pause').title=dashboardBusy&&!d.active_run.can_pause?'Pause this run from its host terminal':'';$('startReview').disabled=dashboardBusy;$('startFull').disabled=dashboardBusy;$('reviewPageRun').disabled=dashboardBusy}
async function startRun(mode){const body={mode},n=parseInt($('limit').value);if(n>0)body.limit=n;if(mode==='reviewed'&&$('stage').value.trim())body.stage=$('stage').value.trim();try{const d=await api('/api/actions/start',{method:'POST',body:JSON.stringify(body)});notify(d.message);setTimeout(refresh,700)}catch(e){notify(e.message,true)}}
async function pauseRun(){if(!confirm('Request a clean, checkpoint-safe pause?'))return;try{const d=await api('/api/actions/pause',{method:'POST',body:'{}'});notify(d.message);refresh()}catch(e){notify(e.message,true)}}
async function showMedia(id){try{const d=await api('/api/media?id='+encodeURIComponent(id));$('detailTitle').textContent=d.media.rel_path;$('detailBody').innerHTML=renderMediaDetail(d);$('detail').showModal()}catch(e){notify(e.message,true)}}
async function runMedia(id){if(!confirm('Retry unsuccessful stages for only this media item?'))return;try{const d=await api('/api/actions/start',{method:'POST',body:JSON.stringify({mode:'single',media_id:id})});$('detail').close();notify(d.message);setTimeout(refresh,700)}catch(e){notify(e.message,true)}}
async function openSettings(){try{const v=await api('/api/settings');$('settingsFields').innerHTML=formFields(v,false);$('settingsmsg').textContent='';$('settings').showModal()}catch(e){notify(e.message,true)}}
async function saveSettings(){try{const d=formData($('settingsFields')),r=await api('/api/settings',{method:'POST',body:JSON.stringify(d)});if(d.dashboard_token){auth=d.dashboard_token;sessionStorage.setItem('mediaManagerToken',auth)}$('settingsmsg').textContent=r.message;notify(r.message)}catch(e){$('settingsmsg').innerHTML='<span class="error">'+esc(e.message)+'</span>'}}
let selectedLibrary='',detailLibrary='';
const singleLibraryApi=api;
api=async function(path,opt={},authorized=true){
  if(authorized&&path!=='/api/libraries'){
    let library=selectedLibrary;
    if(path.startsWith('/api/media')||(path==='/api/actions/start'&&opt.body&&JSON.parse(opt.body).mode==='single'))library=detailLibrary||selectedLibrary;
    if(library)path+=(path.includes('?')?'&':'?')+'library='+encodeURIComponent(library);
  }
  return singleLibraryApi(path,opt,authorized);
};
const singleLibraryReviewTable=reviewTable;
reviewTable=function(rows){return singleLibraryReviewTable(rows.map(r=>({...r,media_id:r.library_id?"'"+r.library_id+":"+Number(r.media_id)+"'":r.media_id})))};
const singleLibraryShowMedia=showMedia;
showMedia=async function(id){const parts=String(id).split(':');detailLibrary=parts.length===2?parts[0]:selectedLibrary;return singleLibraryShowMedia(Number(parts.at(-1)))};
const singleLibraryRefresh=refresh;
refresh=async function(){
  await singleLibraryRefresh();
  const data=await api('/api/libraries');
  if(!$('librarySelect')){
    const controls=document.createElement('div');controls.innerHTML='<label>Library <select id="librarySelect" aria-label="Library"></select></label><span id="libraryQueue" class="muted"></span>';
    $('library').parentElement.appendChild(controls);
    $('librarySelect').onchange=async function(){
      const value=this.value,entry=this.selectedOptions[0];
      if(entry.dataset.registered==='false'){
        if(!confirm('Initialize a separate Media Manager library at this mounted root?')){this.value=selectedLibrary;return}
        try{await api('/api/libraries',{method:'POST',body:JSON.stringify({library_id:value})})}catch(e){notify(e.message,true);this.value=selectedLibrary;return}
      }
      selectedLibrary=value;detailLibrary='';refresh().catch(e=>notify(e.message,true));
    };
  }
  const select=$('librarySelect');
  select.innerHTML='<option value="">Dashboard / default view</option><option value="all">All libraries — combined / queue all</option>'+data.libraries.map(l=>`<option value="${esc(l.id)}" data-registered="${l.registered}">${esc(l.path)}${l.registered?'':' — add library'}</option>`).join('');
  select.value=selectedLibrary;
  $('libraryQueue').textContent=data.error?' Queue stopped: '+data.error:data.busy?' Processing one library; '+data.pending.length+' queued.':'';
  if(data.busy){dashboardBusy=true;['startReview','startFull','reviewPageRun'].forEach(id=>$(id).disabled=true);$('pause').disabled=false}
};
bootstrap().catch(e=>$('authmsg').textContent=e.message);setInterval(()=>{if(!$('app').hidden)refresh().catch(()=>{})},3000);
</script></body></html>"""


def _json_details(value: str | None) -> dict[str, Any]:
    try:
        return json.loads(value or "{}")
    except Exception:
        return {}


class Dashboard:
    def __init__(
        self,
        cfg: LibraryConfig,
        token: str,
        *,
        token_path: Path | None = None,
        setup_required: bool = False,
    ):
        self.cfg = cfg
        self.token = token
        self.token_path = token_path
        self.setup_required = setup_required
        self._lock = threading.Lock()
        self._child: subprocess.Popen[bytes] | None = None

    def authorized(self, header: str | None) -> bool:
        prefix = "Bearer "
        supplied = header[len(prefix):] if header and header.startswith(prefix) else ""
        return bool(self.token) and secrets.compare_digest(supplied, self.token)

    def bootstrap(self) -> dict[str, Any]:
        defaults = self.settings() if self.setup_required else {}
        # Bootstrap is intentionally unauthenticated. Never disclose an
        # account name or even the existence of stored provider secrets.
        if defaults:
            defaults.update({
                "opensubtitles_username": "",
                "opensubtitles_api_key_configured": False,
                "opensubtitles_password_configured": False,
            })
        return {
            "setup_required": self.setup_required,
            "library": str(self.cfg.root) if self.setup_required else "Configured library",
            "defaults": defaults,
        }

    def settings(self) -> dict[str, Any]:
        return {
            "movies_directory": self.cfg["library"]["movies_directory"],
            "tv_directory": self.cfg["library"]["tv_directory"],
            "subtitle_languages": self.cfg["subtitles"]["languages"],
            "av_sync_enabled": bool(self.cfg["av_sync"]["enabled"]),
            "crop_enabled": bool(self.cfg["crop"]["enabled"]),
            "subtitles_enabled": bool(self.cfg["subtitles"]["enabled"]),
            "syncnet_device": self.cfg["av_sync"]["syncnet_device"],
            "opensubtitles_username": self.cfg["opensubtitles"].get("username", ""),
            "opensubtitles_api_key_configured": bool(self.cfg["opensubtitles"].get("api_key")),
            "opensubtitles_password_configured": bool(self.cfg.get_opensubtitles_password()),
        }

    @staticmethod
    def _directory(value: Any, label: str) -> str:
        text = str(value or "").strip()
        path = Path(text)
        if not text or text == "." or path.is_absolute() or ".." in path.parts:
            raise MediaManagerError(f"{label} must be a relative folder inside the mounted library")
        return path.as_posix()

    def _active_run(self) -> bool:
        # The library lock is shared through Docker's bind mount, whereas
        # host PIDs are not visible inside the container's PID namespace.
        try:
            with (self.cfg.state_dir / "manager.lock").open("r") as handle:
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
                fcntl.flock(handle, fcntl.LOCK_UN)
        except FileNotFoundError:
            pass
        return False

    def update_settings(self, payload: dict[str, Any], *, initial: bool = False) -> dict[str, Any]:
        with self._lock:
            if initial:
                if not self.setup_required:
                    raise MediaManagerError("Dashboard setup has already been completed")
            if self._active_run():
                raise MediaManagerError("Pause the active run before changing settings")

            token = str(payload.get("dashboard_token") or "").strip()
            if initial and len(token) < 16:
                raise MediaManagerError("Dashboard token must contain at least 16 characters")
            if token and len(token) < 16:
                raise MediaManagerError("New dashboard token must contain at least 16 characters")
            if token and self.token_path is None:
                raise MediaManagerError(
                    "The dashboard token is controlled by MEDIA_MANAGER_WEB_TOKEN; remove that environment variable to manage it here"
                )

            self.cfg["library"]["movies_directory"] = self._directory(
                payload.get("movies_directory", self.cfg["library"]["movies_directory"]), "Movies folder"
            )
            self.cfg["library"]["tv_directory"] = self._directory(
                payload.get("tv_directory", self.cfg["library"]["tv_directory"]), "TV folder"
            )
            languages = payload.get("subtitle_languages", self.cfg["subtitles"]["languages"])
            if isinstance(languages, str):
                languages = [item.strip() for item in languages.split(",") if item.strip()]
            if not isinstance(languages, list) or not languages or any(not isinstance(item, str) for item in languages):
                raise MediaManagerError("Provide at least one subtitle language")
            self.cfg["subtitles"]["languages"] = languages
            for section, key in (("av_sync", "av_sync_enabled"), ("crop", "crop_enabled"), ("subtitles", "subtitles_enabled")):
                if key in payload:
                    if not isinstance(payload[key], bool):
                        raise MediaManagerError(f"{key} must be true or false")
                    self.cfg[section]["enabled"] = payload[key]
            device = str(payload.get("syncnet_device", self.cfg["av_sync"]["syncnet_device"])).strip().lower()
            if device not in {"cpu", "cuda"}:
                raise MediaManagerError("SyncNet device must be cpu or cuda")
            self.cfg["av_sync"]["syncnet_device"] = device
            username = str(payload.get("opensubtitles_username", self.cfg["opensubtitles"].get("username", ""))).strip()
            self.cfg["opensubtitles"]["username"] = username
            api_key = str(payload.get("opensubtitles_api_key") or "").strip()
            if api_key:
                self.cfg["opensubtitles"]["api_key"] = api_key
            self.cfg.save()
            password = str(payload.get("opensubtitles_password") or "")
            if password:
                self.cfg.set_opensubtitles_password(password)
            if token:
                assert self.token_path is not None
                _write_token(self.token_path, token)
                self.token = token
            if initial:
                self.setup_required = False
            return {"message": "Setup complete" if initial else "Settings saved"}

    def status(self) -> dict[str, Any]:
        with Database(self.cfg.state_dir / "media-manager.db") as db:
            counts = {str(row["overall_status"]): int(row["n"]) for row in db.conn.execute(
                "SELECT overall_status,COUNT(*) n FROM media WHERE present=1 GROUP BY overall_status"
            )}
            active = db.active_run()
            current = db.conn.execute(
                """SELECT m.id media_id,m.rel_path,s.stage,s.details_json,s.updated_at
                   FROM stages s JOIN media m ON m.id=s.media_id
                   WHERE s.status='running' ORDER BY s.updated_at DESC LIMIT 1"""
            ).fetchone()
            review_rows = db.conn.execute(
                """SELECT m.id media_id,m.rel_path,s.stage,s.status,s.details_json
                   FROM stages s JOIN media m ON m.id=s.media_id
                   WHERE m.present=1 AND s.status IN ('needs_review','insufficient_evidence','failed')
                   ORDER BY m.rel_path,s.stage LIMIT 250"""
            ).fetchall()
            reviews = []
            for row in review_rows:
                details = _json_details(row["details_json"])
                reviews.append({
                    "media_id": int(row["media_id"]), "path": row["rel_path"],
                    "stage": row["stage"], "status": row["status"],
                    "reason": details.get("reason") or details.get("error") or details.get("correction_error") or "",
                })
            runs = [dict(row) for row in db.conn.execute(
                "SELECT id,started_at,ended_at,status,pid,hostname,reason FROM runs ORDER BY id DESC LIMIT 20"
            )]
            active_data = dict(active) if active else None
            if active_data and not self._active_run():
                active_data = None
            if active_data:
                from .cli import _pid_matches_library
                active_data["can_pause"] = (
                    active_data.get("hostname") == socket.gethostname()
                    and _pid_matches_library(int(active_data.get("pid") or 0), self.cfg)
                )
            current_data = None
            if current and active_data:
                details = _json_details(current["details_json"])
                current_data = {
                    "media_id": int(current["media_id"]), "path": current["rel_path"],
                    "stage": current["stage"], "phase": details.get("phase"),
                    "updated_at": current["updated_at"], "details": details,
                }
            return {
                "library": str(self.cfg.root), "counts": counts, "active_run": active_data,
                "current_stage": current_data, "reviews": reviews, "runs": runs,
            }

    def media(self, media_id: int) -> dict[str, Any]:
        with Database(self.cfg.state_dir / "media-manager.db") as db:
            row = db.get_media(media_id)
            media = {key: row[key] for key in row.keys() if key != "probe_json"}
            stages = {
                name: {"status": stage["status"], "updated_at": stage["updated_at"],
                       "tool_version": stage["tool_version"], "details": _json_details(stage["details_json"])}
                for name, stage in db.stages(media_id).items()
            }
            events = [dict(item) for item in db.conn.execute(
                "SELECT created_at,kind,message,details_json FROM events WHERE media_id=? ORDER BY id DESC LIMIT 100",
                (media_id,),
            )]
            for event in events:
                event["details"] = _json_details(event.pop("details_json", "{}"))
            return {"media": media, "stages": stages, "events": events}

    def start(self, payload: dict[str, Any]) -> dict[str, Any]:
        mode = str(payload.get("mode") or "reviewed")
        if mode not in {"reviewed", "full", "single"}:
            raise MediaManagerError("mode must be 'reviewed', 'full', or 'single'")
        media_id: int | None = None
        if mode == "single":
            try:
                media_id = int(payload.get("media_id"))
            except (TypeError, ValueError) as exc:
                raise MediaManagerError("A valid media ID is required for a single-item run") from exc
            if media_id <= 0:
                raise MediaManagerError("A valid media ID is required for a single-item run")
        with self._lock:
            if self._child is not None and self._child.poll() is None:
                raise MediaManagerError("A dashboard-started run is already active")
            with Database(self.cfg.state_dir / "media-manager.db") as db:
                active = db.active_run()
                if self._active_run():
                    raise MediaManagerError("The library is busy with another media-manager operation")
                if media_id is not None:
                    try:
                        media_row = db.get_media(media_id)
                    except KeyError as exc:
                        raise MediaManagerError("The selected media item no longer exists") from exc
                    if not int(media_row["present"]):
                        raise MediaManagerError("The selected media item is no longer present in the library")
            command = [sys.executable, "-m", "media_manager", "run"]
            if mode in {"reviewed", "single"}:
                command.append("--reviewed-only")
            if media_id is not None:
                command += ["--media-id", str(media_id)]
            limit = payload.get("limit")
            if limit is not None:
                try:
                    parsed = int(limit)
                except (TypeError, ValueError) as exc:
                    raise MediaManagerError("limit must be a positive integer") from exc
                if parsed <= 0:
                    raise MediaManagerError("limit must be a positive integer")
                command += ["--limit", str(parsed)]
            stage = str(payload.get("stage") or "").strip()
            if stage:
                if mode not in {"reviewed", "single"}:
                    raise MediaManagerError("stage filtering is available only in reviewed mode")
                command += ["--review-stage", stage]
            log_path = self.cfg.state_dir / "logs" / "web-run.log"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            log = log_path.open("ab", buffering=0)
            try:
                self._child = subprocess.Popen(
                    command, cwd=self.cfg.root, stdin=subprocess.DEVNULL,
                    stdout=log, stderr=subprocess.STDOUT, start_new_session=True,
                )
            finally:
                log.close()
            message = "Selected item retry started" if mode == "single" else "Run started"
            return {"message": message, "pid": self._child.pid, "mode": mode, "log": str(log_path)}

    def pause(self) -> dict[str, Any]:
        active = self.status()["active_run"]
        if active and not active["can_pause"]:
            raise MediaManagerError("Pause this run from the host terminal where it was started")
        command = [sys.executable, "-m", "media_manager", "pause", "--timeout", "30"]
        result = subprocess.run(command, cwd=self.cfg.root, capture_output=True, text=True, timeout=45)
        message = (result.stdout or result.stderr or "Pause request completed").strip()
        if result.returncode != 0:
            raise MediaManagerError(message)
        return {"message": message}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        stat = Path(f"/proc/{pid}/stat").read_text(errors="replace")
        return ") Z " not in stat
    except (OSError, ValueError):
        return False


def _handler(dashboard: Dashboard, hub=None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "media-manager-dashboard"

        def _send_json(self, status: int, data: dict[str, Any]) -> None:
            body = json.dumps(data, ensure_ascii=False, default=str).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers(); self.wfile.write(body)

        def _authorized(self) -> bool:
            if dashboard.authorized(self.headers.get("Authorization")):
                return True
            self._send_json(HTTPStatus.UNAUTHORIZED, {"error": "Invalid or missing dashboard token"})
            return False

        def do_GET(self) -> None:
            parsed = urlparse(self.path)
            if parsed.path == "/healthz":
                self._send_json(HTTPStatus.OK, {"status": "ok"}); return
            if parsed.path == "/":
                body = INDEX_HTML.encode()
                self.send_response(HTTPStatus.OK)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; frame-ancestors 'none'")
                self.send_header("X-Frame-Options", "DENY")
                self.send_header("Cache-Control", "no-store")
                self.end_headers(); self.wfile.write(body); return
            if parsed.path == "/api/bootstrap":
                self._send_json(HTTPStatus.OK, dashboard.bootstrap()); return
            if not self._authorized(): return
            try:
                library = parse_qs(parsed.query).get("library", [None])[0]
                if parsed.path == "/api/libraries" and hub:
                    self._send_json(HTTPStatus.OK, hub.listing()); return
                if parsed.path == "/api/status":
                    self._send_json(HTTPStatus.OK, hub.status(library) if hub else dashboard.status()); return
                target = hub.target(library) if hub else dashboard
                if parsed.path == "/api/settings":
                    self._send_json(HTTPStatus.OK, target.settings()); return
                if parsed.path == "/api/media":
                    values = parse_qs(parsed.query).get("id", [])
                    if not values: raise MediaManagerError("Missing media id")
                    self._send_json(HTTPStatus.OK, target.media(int(values[0]))); return
                self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"})
            except (MediaManagerError, KeyError, ValueError) as exc:
                self._send_json(HTTPStatus.BAD_REQUEST, {"error": str(exc)})

        def do_POST(self) -> None:
            parsed = urlparse(self.path)
            origin = self.headers.get("Origin")
            host = self.headers.get("Host")
            if origin and host and urlparse(origin).netloc != host:
                self._send_json(HTTPStatus.FORBIDDEN, {"error": "Cross-origin request rejected"}); return
            is_setup = parsed.path == "/api/setup"
            if not is_setup and not self._authorized(): return
            try:
                length = max(0, min(int(self.headers.get("Content-Length", "0") or 0), 65536))
                payload = json.loads(self.rfile.read(length) or b"{}")
                if not isinstance(payload, dict): raise MediaManagerError("JSON body must be an object")
                library = parse_qs(parsed.query).get("library", [None])[0]
                if is_setup: result = dashboard.update_settings(payload, initial=True)
                elif parsed.path == "/api/libraries" and hub: result = hub.add(payload)
                elif parsed.path == "/api/settings":
                    target = hub.target(library) if hub else dashboard
                    if target is not dashboard and payload.get("dashboard_token"):
                        raise MediaManagerError("Change the shared token from Dashboard / default view settings")
                    result = target.update_settings(payload)
                elif parsed.path == "/api/actions/start": result = hub.start(payload, library) if hub else dashboard.start(payload)
                elif parsed.path == "/api/actions/pause": result = hub.pause(library) if hub else dashboard.pause()
                else:
                    self._send_json(HTTPStatus.NOT_FOUND, {"error": "Not found"}); return
                self._send_json(HTTPStatus.OK, result)
            except (MediaManagerError, json.JSONDecodeError, subprocess.TimeoutExpired) as exc:
                self._send_json(HTTPStatus.CONFLICT, {"error": str(exc)})

        def log_message(self, fmt: str, *args: Any) -> None:
            sys.stderr.write("dashboard: " + fmt % args + "\n")

    return Handler


def _write_token(path: Path, token: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, stat.S_IRUSR | stat.S_IWUSR)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            fd = -1
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp, path)
    finally:
        if fd >= 0:
            os.close(fd)
        if tmp.exists():
            tmp.unlink()


def load_or_create_token(cfg: LibraryConfig, token_file: Path | None = None) -> tuple[str, Path | None, bool]:
    """Load dashboard access, leaving a fresh install for browser setup.

    The third return value means setup is required. The historical function
    name is retained for callers from earlier releases.
    """
    env_token = os.environ.get("MEDIA_MANAGER_WEB_TOKEN", "").strip()
    if env_token:
        if len(env_token) < 16:
            raise MediaManagerError("MEDIA_MANAGER_WEB_TOKEN must contain at least 16 characters")
        return env_token, None, False
    path = token_file or (cfg.state_dir / "web-token")
    if path.exists():
        token = path.read_text(encoding="utf-8").strip()
        if len(token) < 16:
            raise MediaManagerError(f"Dashboard token in {path} is too short")
        return token, path, False
    return "", path, True


def serve(cfg: LibraryConfig, *, host: str, port: int, token_file: Path | None = None, standalone: bool = False) -> None:
    token, path, setup_required = load_or_create_token(cfg, token_file)
    if path is not None:
        print(f"Dashboard token file: {path}")
    if setup_required:
        print("First-time dashboard setup is required in the browser")
    print(f"Dashboard: http://{host}:{port}")
    dashboard = Dashboard(cfg, token, token_path=path, setup_required=setup_required)
    from .libraries import LibraryHub, configured_libraries
    hub = LibraryHub(dashboard, configured_libraries(), standalone=standalone)
    server = ThreadingHTTPServer((host, port), _handler(dashboard, hub))
    server.daemon_threads = True
    old_handler = signal.getsignal(signal.SIGTERM)

    def stop_handler(_signum, _frame):
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, stop_handler)
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        signal.signal(signal.SIGTERM, old_handler)
        if hub.worker is not None and hub.worker.is_alive():
            try:
                hub.pause()
            except Exception as exc:
                print(f"Dashboard shutdown could not cleanly pause the child run: {exc}", file=sys.stderr)
