"""Loopback-only dashboard for trader ranking, copy control and manual exits."""
from __future__ import annotations
import json
import logging
import secrets
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from queue import Queue

logger = logging.getLogger(__name__)

_PAGE = r"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Pump.fun Copy Trader</title>
<style>
:root{color-scheme:dark;--bg:#0b0e13;--panel:#131923;--line:#273243;--text:#eef3fa;--muted:#9aa9bc;--green:#2bd576;--red:#ff6b72}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 system-ui,Segoe UI,sans-serif}
main{max-width:1100px;margin:auto;padding:24px}header,.toolbar,.metrics{display:flex;gap:12px;align-items:center;justify-content:space-between;flex-wrap:wrap}
h1{font-size:26px;margin:0}h2{font-size:17px;margin:0 0 14px}.muted{color:var(--muted)}.pill{border:1px solid var(--line);border-radius:999px;padding:4px 10px;color:var(--muted)}
.grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:10px;margin:16px 0}
.panel,.metric{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:16px;margin-bottom:14px}.metric small{color:var(--muted);display:block}.metric strong{font-size:22px;display:block;margin-top:4px}
button,select{background:#202b3a;color:var(--text);border:1px solid #38485e;border-radius:8px;padding:9px 12px;font:inherit;cursor:pointer}
button.primary{background:#146e49;border-color:#23835c}button.danger{background:#702a35;border-color:#97404b}
table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:10px 8px;border-bottom:1px solid var(--line);vertical-align:middle}th{font-size:12px;text-transform:uppercase;color:var(--muted)}
.scroll{overflow-x:auto}.addr{font-family:ui-monospace,Consolas,monospace;font-size:12px;word-break:break-all}.green{color:var(--green)}.red{color:var(--red)}
.status{border:1px solid var(--line);border-radius:10px;padding:10px 12px;color:var(--muted);margin:12px 0}.empty{padding:22px;text-align:center;color:var(--muted)}.footer{font-size:12px;color:var(--muted);margin-top:20px}
@media(max-width:760px){main{padding:14px}.grid{grid-template-columns:repeat(2,minmax(0,1fr))}.hide-sm{display:none}}
</style></head>
<body><main>
<header><div><h1>Memecoin <span class="muted">/ Pump.fun</span></h1><div class="muted">Top traders · automatic copy · manual exits</div></div><div><span class="pill" id="mode">MODE</span> <button id="refresh">Refresh</button></div></header>
<div id="message" class="status">Connecting to local bot...</div>
<div class="grid"><div class="metric"><small>Estimated equity</small><strong id="equity">—</strong></div>
<div class="metric"><small>Open positions</small><strong id="posCount">0</strong></div><div class="metric"><small>Auto-copy</small><strong id="copyStatus">OFF</strong></div>
<div class="metric"><small>Active traders</small><strong id="walletCount">0</strong></div></div>
 <section class="panel"><h2>Trade-feed diagnostics</h2><div id="feedSummary" class="status">Checking feed status…</div>
 <div id="feedDetail" class="muted">Waiting for feed counters…</div><div id="rpcFeedDetail" class="muted" style="margin-top:8px">Waiting for Solana logs feed…</div><div id="signalDetail" class="muted" style="margin-top:8px"></div></section>
<section class="panel"><div class="toolbar"><div><h2>Independent token discovery <span class="pill" id="discoveryMode">SHADOW ONLY</span></h2><div class="muted">Live execution is a separate opt-in. Swap input is capped at $0.50; actual wallet debit can be higher when network fees or new token-account rent apply. At most one discovery position can be open.</div></div></div>
 <div id="discoverySummary" class="status">Checking independent scanner…</div>
 <div id="opportunityFunnel" class="status">Building opportunity and execution funnel…</div>
 <div class="scroll"><table><thead><tr><th>Token</th><th>Status / score</th><th>Real curve liquidity</th><th>Entry MC</th><th>Current MC</th><th>Holder exposure</th><th>Buyers / flow</th><th>Shadow return</th><th>Decision</th></tr></thead><tbody id="discoveryCandidates"><tr><td colspan="9" class="empty">No discovery candidates recorded.</td></tr></tbody></table></div>
 <div class="footer">Token creation events are free from PumpPortal; token-specific trade subscriptions are metered. Entry screens are not guarantees. Market-cap values prefixed with ~ are estimates from price × total supply (FDV-style), not guaranteed realizable value. Live discovery is off by default and only opens a trade after fresh on-chain rechecks and global risk gates. The $0.50 cap applies to swap input; actual wallet debit can be higher due to network fees or account rent. Only one discovery position can be open.</div></section>
<section class="panel"><div class="toolbar"><div><h2>Trending Solana tokens <span class="pill">ANALYSIS ONLY</span></h2><div class="muted">Trending pools screened with two market-data sources, direct mint-account checks and RugCheck. No buys are submitted by this scanner.</div></div></div>
 <div id="trendingSummary" class="status">Trending scanner disabled.</div>
 <div class="scroll"><table><thead><tr><th>Token / pool</th><th>Momentum</th><th>Liquidity</th><th>Volume 1h / 24h</th><th>Price move 5m / 1h / 6h / 24h</th><th>Mint / RugCheck</th><th>Holder / LP risk</th><th>Decision</th></tr></thead><tbody id="trendingCandidates"><tr><td colspan="8" class="empty">Enable TRENDING_DISCOVERY_ENABLED to scan trending Solana tokens.</td></tr></tbody></table></div>
 <div class="footer">SCREENED means the available checks passed configured thresholds, not that a token is guaranteed safe. Missing reports, unknown extensions or conflicting market data are shown for review. Trending-token auto-trading is intentionally disabled until a compatible execution route is implemented and tested.</div></section>
<section class="panel"><div class="toolbar"><div><h2>Top Pump.fun traders</h2><div class="muted">Choose a period, then copy its current top 10.</div></div>
<div><select id="window"><option value="overall">Overall composite</option><option value="month">Month (1M)</option><option value="week">Week (1W)</option><option value="day" selected>Day (1D)</option></select>
<button class="primary" id="copyTop">Copy this Top 10</button> <button id="stopCopy">Stop copying</button></div></div>
<div class="scroll"><table><thead><tr><th>#</th><th>Wallet</th><th class="hide-sm">Profit / score</th><th>Action</th></tr></thead><tbody id="leaders"><tr><td colspan="4" class="empty">No leaderboard loaded.</td></tr></tbody></table></div>
<div class="footer">Pump.fun displays 1D, 1W and 1M. Overall is a composite rank across those lists, not a verified all-time ranking.</div></section>
<section class="panel"><div class="toolbar"><div><h2>Open positions</h2><div class="muted">Manual exit submits an exit attempt through the running bot.</div></div><button class="danger" id="exitAll">Manual exit all</button></div>
<div class="scroll"><table><thead><tr><th>Mint</th><th>Entry price</th><th>Mark price</th><th>Entry MC</th><th>Current MC</th><th>Move</th><th>Action</th></tr></thead><tbody id="positions"><tr><td colspan="7" class="empty">No open positions.</td></tr></tbody></table></div></section>
<section class="panel"><h2>Recent trades</h2><div class="scroll"><table><thead><tr><th>Time</th><th>Strategy</th><th>Action</th><th>Mint</th><th>Value</th><th>P&amp;L</th><th>Reason</th></tr></thead><tbody id="trades"></tbody></table></div></section>
<div class="footer">Local-only dashboard. Exits are attempts, not guaranteed fills. Never share your seed phrase or private key.</div></main>
<script>
var controlToken = "__CONTROL_TOKEN__";
function el(id){return document.getElementById(id)}
function short(s){if(!s)return "—";return s.length>16?s.slice(0,7)+"…"+s.slice(-6):s}
function num(x){return Number.isFinite(Number(x))?Number(x).toFixed(4):"—"}
function cap(x,source){var v=Number(x);if(!Number.isFinite(v)||v<=0)return "—";var s=v>=1e9?"$"+(v/1e9).toFixed(2)+"B":v>=1e6?"$"+(v/1e6).toFixed(2)+"M":v>=1e3?"$"+(v/1e3).toFixed(2)+"K":"$"+v.toFixed(0);return String(source||"").toLowerCase().includes("estimated")?"~"+s:s}
async function action(body){try{var r=await fetch("/api/action",{method:"POST",headers:{"Content-Type":"application/json","X-Control-Token":controlToken},body:JSON.stringify(body)});var d=await r.json();el("message").textContent=d.message||"Command queued";await refreshState()}catch(e){el("message").textContent="Local bot not responding: "+e}}
function render(d){
 el("mode").textContent=(d.mode||"unknown").toUpperCase();el("equity").textContent=d.equity_usd===null?"—":"$"+num(d.equity_usd);
 el("posCount").textContent=(d.positions||[]).length;el("copyStatus").textContent=d.auto_copy?"ON":"OFF";el("walletCount").textContent=(d.active_wallets||[]).length;
 var f=d.feed_stats||{};var ss=d.signal_stats||{};
  function ago(ts){if(!ts)return "never";var sec=Math.max(0,Math.floor(Date.now()/1000-Number(ts)));return sec<60?sec+"s ago":Math.floor(sec/60)+"m "+(sec%60)+"s ago"}
  el("feedSummary").textContent=(f.connected?"WebSocket connected":"WebSocket disconnected")+" · "+(f.subscribed_wallet_count||0)+" account wallets requested · "+(f.account_trade_events_received||0)+" account trade events";
  el("feedDetail").textContent="Messages received: "+(f.messages_received||0)+" · Trade events: "+(f.trade_events_received||0)+" · Last message: "+ago(f.last_message_at)+" · Last account trade: "+ago(f.last_account_trade_at);
  var r=d.solana_logs_stats||{};
  el("rpcFeedDetail").textContent="RPC log feed: "+(!r.enabled?"disabled":(r.connected?"connected":"disconnected"))+" · "+(r.subscribed_wallet_count||0)+" wallets subscribed · "+(r.notifications_received||0)+" notifications · "+ago(r.last_notification_at);
  var notices=[];if(f.error)notices.push("Feed error: "+f.error);if(f.last_notice)notices.push("Provider notice: "+f.last_notice);if(f.last_event_summary)notices.push("Last event: "+f.last_event_summary);if(r.error)notices.push("RPC log feed error: "+r.error);
  el("signalDetail").textContent=notices.join(" · ")+" · Parsed signals: "+(ss.parsed_signals||0)+" · Unparsed: "+(ss.unparsed_events||0)+" · Duplicate: "+(ss.duplicate_events||0)+" · Unwatched: "+(ss.unwatched_events||0)+" · Sells with no position: "+(ss.sell_without_position||0)+" · RPC-derived signals: "+(ss.rpc_log_signals||0);
  var td=d.token_discovery||{};
  el("discoveryMode").textContent=td.live_enabled?"LIVE EXECUTION ON":"SHADOW ONLY";
  el("discoverySummary").textContent=td.enabled
    ? "SCANNER ENABLED · "+(td.live_enabled?("LIVE DISCOVERY ON · max swap input $"+num(td.live_entry_cap_usd)+" · open discovery positions "+(td.live_open_positions||0)+"/"+(td.live_position_limit||1)):"LIVE EXECUTION OFF · shadow entries only")+" · Launch events "+(td.launch_events||0)+" · On-chain safety scans "+(td.safety_scans||0)+" · Safety rejected "+(td.safety_rejected||0)+" · Low-liquidity rejected "+(td.liquidity_rejected||0)+" · Skipped (capacity/rate) "+((td.capacity_skips||0)+(td.rate_limit_skips||0))+" · Queue "+(td.queue_count||0)+"/"+(td.queue_limit||100)+" · Queue expired/dropped "+(td.queue_expired||0)+"/"+(td.queue_dropped||0)+" · Recent-list backfill polls/errors "+(td.backfill_polls||0)+"/"+(td.backfill_errors||0)+" · Max launch age "+(td.max_token_age_hours||24)+"h · Shadow entries "+(td.shadow_entries||0)+" · Active token subscriptions "+(td.active_watch_count||0)+"/"+(td.max_watched_tokens||0)+" · Min verified curve liquidity $"+num(td.min_liquidity_usd)+" · Score threshold "+(td.min_score||0)+" · Buyer floor "+(td.min_unique_buyers||8)
    : "SCANNER DISABLED · Set TOKEN_DISCOVERY_ENABLED=true to enable screening. Live execution additionally requires TOKEN_DISCOVERY_LIVE_ENABLED=true, BOT_MODE=live and explicit live confirmation.";
  el("opportunityFunnel").textContent = "Tracked launches: "+(td.tracked_candidates||0)+" · Queued: "+(td.queue_count||0)+" · On-chain safety passed: "+(td.safety_passed_count||0)+" · Entry gates passed: "+(td.qualified_count||0)+" · Live attempts: "+(td.live_attempted_count||0)+" · Confirmed live buys: "+(td.live_confirmed_count||0)+" · Failed/blocked/stale: "+(td.live_failed_count||0)+" · Unfilled shadow candidates later peaking +20% or more: "+(td.unfilled_peak_20_count||0)+" (hypothetical movement, not realized profit).";
  function esc(v){return String(v===undefined||v===null?"":v).replace(/[&<>"']/g,function(ch){return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[ch]})}
  var dc=td.candidates||[];
  el("discoveryCandidates").innerHTML=dc.length?dc.map(function(x){
    var move=x.shadow_entry_price_usd?Number(x.current_return_pct||0):null;
    var ret=move===null?"—":((move>=0?"+":"")+move.toFixed(1)+"%")+" (peak "+((Number(x.max_return_pct||0)>=0?"+":"")+Number(x.max_return_pct||0).toFixed(1)+"%)");
    if(move!==null){var marks=["1m","5m","15m"].map(function(k){var v=x["return_"+k+"_pct"];return k+": "+(v===null||v===undefined?"pending":((Number(v)>=0?"+":"")+Number(v).toFixed(1)+"%"))});ret+=" · "+marks.join(" / ")}
    var status=String(x.status||"—").replace(/_/g," ");
    var score=Number(x.score||0);
    var age=ago(x.created_at);
    var liquidity=Number(x.real_liquidity_usd||0);
    var flow=Number(x.buy_volume_sol||0)-Number(x.sell_volume_sol||0);
    var holders=x.top5_holder_supply_pct===undefined?"—":num(x.top5_holder_supply_pct)+"% top 5 / "+num(x.creator_supply_pct)+"% creator";
    var decision=String(x.reason||"—");
    if(x.live_entry_status){decision+=" · live "+String(x.live_entry_status).replace(/_/g," ");if(x.live_entry_reason)decision+=": "+x.live_entry_reason}
    var entryMcSource=x.entry_market_cap_source||"Market cap unavailable";
    var currentMcSource=x.market_cap_source||"Market cap unavailable";
    var entryMc=cap(x.entry_market_cap_usd,entryMcSource);
    var currentMc=cap(x.market_cap_usd,currentMcSource);
    return "<tr><td><div>"+esc(x.name||x.symbol||"Unnamed token")+"</div><div class='addr' title='"+esc(x.mint)+"'>"+short(esc(x.mint))+"</div><a class='muted' target='_blank' rel='noreferrer' href='https://pump.fun/coin/"+encodeURIComponent(x.mint||"")+"'>Pump.fun ↗</a></td><td>"+esc(status)+" · "+score+"/100<div class='muted'>Launch/seen "+esc(age)+"</div></td><td>$"+num(liquidity)+"</td><td title='"+esc(entryMcSource)+"'>"+esc(entryMc)+"</td><td title='"+esc(currentMcSource)+"'>"+esc(currentMc)+"</td><td>"+esc(holders)+"</td><td>"+(x.unique_buyers||0)+" · "+flow.toFixed(3)+" SOL net</td><td class='"+(move===null?"":(move>=0?"green":"red"))+"'>"+esc(ret)+"</td><td>"+esc(decision)+"</td></tr>";
  }).join(""):"<tr><td colspan='9' class='empty'>No discovery candidates recorded yet.</td></tr>";
  var tr=d.trending_discovery||{};
  el("trendingSummary").textContent=tr.enabled
    ? "TRENDING SCANNER ENABLED · live entries "+(tr.live_enabled?"ON":"OFF")+" · max swap input $"+num(tr.live_entry_cap_usd||0.50)+" · score floor "+(tr.min_score||75)+"/100 · max price impact "+num(tr.max_price_impact_pct||5)+"% · last scan "+ago(tr.last_scan_at)+" · pools "+(tr.pool_count||0)+" · checked "+(tr.checked_count||0)+" · screened "+(tr.screened_count||0)+" · rejected/filtered "+(tr.rejected_count||0)+" · liquidity floor $"+num(tr.min_liquidity_usd)+" · 1h volume floor $"+num(tr.min_volume_1h_usd)+" · interval "+(tr.poll_seconds||60)+"s"+(tr.last_error?" · Last error: "+tr.last_error:"")
    : "TRENDING SCANNER DISABLED · Set TRENDING_DISCOVERY_ENABLED=true to enable screening.";
  var trendRows=tr.candidates||[];
  el("trendingCandidates").innerHTML=trendRows.length?trendRows.map(function(x){
    var changes=["change_5m_pct","change_1h_pct","change_6h_pct","change_24h_pct"].map(function(k){var v=x[k];return v===undefined||v===null?"—":((Number(v)>=0?"+":"")+Number(v).toFixed(1)+"%")});
    var status=String(x.status||"UNKNOWN");
    var statusClass=status==="SCREENED"?"green":(status==="REJECTED"||status==="FILTERED"?"red":"");
    var top=x.top_holders_pct===undefined||x.top_holders_pct===null?"—":Number(x.top_holders_pct).toFixed(1)+"%";
    var lp=x.lp_locked_pct===undefined||x.lp_locked_pct===null?"—":Number(x.lp_locked_pct).toFixed(1)+"%";
    var flags=(x.risk_flags||[]).join(" · ");
    var liveDecision=x.live_entry_status?"Live: "+String(x.live_entry_status).replace(/_/g," ")+(x.live_entry_reason?" · "+x.live_entry_reason:""):"";
    var poolUrl=x.dexscreener_url||("https://dexscreener.com/solana/"+encodeURIComponent(x.dexscreener_pair_address||x.pool_address||""));
    var rugUrl="https://rugcheck.xyz/tokens/"+encodeURIComponent(x.mint||"");
    var tokenLabel=(x.name||x.symbol||"Unknown token")+" ("+(x.symbol||"—")+")";
    return "<tr><td><div>"+esc(tokenLabel)+"</div><div class='addr' title='"+esc(x.mint)+"'>"+esc(short(x.mint))+"</div><div class='muted'>"+esc(x.pool_name||"Solana pool")+"</div><a target='_blank' rel='noreferrer' href='"+poolUrl+"'>DEX Screener ↗</a></td><td>"+Number(x.momentum_score||0)+"/100<div class='muted'>"+esc(ago(x.updated_at))+"</div></td><td>$"+num(x.liquidity_usd)+"<div class='muted'>Gecko "+(x.gecko_liquidity_usd===undefined?"—":"$"+num(x.gecko_liquidity_usd))+" / DEX "+(x.dexscreener_liquidity_usd===undefined?"—":"$"+num(x.dexscreener_liquidity_usd))+"</div></td><td>$"+num(x.volume_1h_usd)+" / $"+num(x.volume_24h_usd)+"</td><td>"+changes.join(" / ")+"</td><td>"+esc(x.onchain_status||"—")+" · "+esc(x.rugcheck_status||"not checked")+"<div><a target='_blank' rel='noreferrer' href='"+rugUrl+"'>RugCheck ↗</a></div></td><td>Top holders "+top+" · LP locked/burned "+lp+"</td><td><strong class='"+statusClass+"'>"+esc(status)+"</strong><div>"+esc(x.decision||"")+"</div>"+(liveDecision?"<div class='muted'>"+esc(liveDecision)+"</div>":"")+"<div class='muted'>"+esc(flags)+"</div></td></tr>";
  }).join(""):"<tr><td colspan='8' class='empty'>No trending candidates yet. Enable TRENDING_DISCOVERY_ENABLED in .env and wait for the first scan.</td></tr>";
  var period=el("window").value;var leaders=((d.leaderboards||{})[period]||[]);
 el("leaders").innerHTML=leaders.length?leaders.map(function(x,i){var score=period==="overall"?"Composite "+(x.rank_score||""):x.profit===null||x.profit===undefined?"—":num(x.profit);return "<tr><td>"+(x.rank||i+1)+"</td><td><div class='addr' title='"+x.wallet+"'>"+short(x.wallet)+"</div><a class='muted' target='_blank' rel='noreferrer' href='https://pump.fun/profile/"+x.wallet+"'>Profile ↗</a></td><td class='hide-sm'>"+score+"</td><td>"+(d.auto_copy&&(d.active_wallets||[]).includes(x.wallet)?"Copying":((d.active_wallets||[]).includes(x.wallet)?"Monitoring open position":"Not copying"))+"</td></tr>"}).join(""):"<tr><td colspan='4' class='empty'>No leaderboard loaded. Check API access/configuration.</td></tr>";
 var ps=d.positions||[];el("positions").innerHTML=ps.length?ps.map(function(p){var m=Number(p.move_pct||0);var st=p.strategy==="token_discovery"?"Live discovery":(p.strategy==="trending_discovery"?"Trending live":"Leader copy");var ems=p.entry_market_cap_source||"Market cap unavailable";var cms=p.current_market_cap_source||"Market cap unavailable";var exitState=p.exit_blocked_reason?"<div class='red' title='"+esc(p.exit_blocked_reason)+"'>Zero balance reported · reconciliation required</div>":"";return "<tr><td><div class='addr' title='"+p.mint+"'>"+short(p.mint)+"</div><div class='muted'>"+st+"</div><a target='_blank' rel='noreferrer' href='"+(p.strategy==="trending_discovery"?"https://dexscreener.com/solana/":"https://pump.fun/coin/")+p.mint+"'>"+(p.strategy==="trending_discovery"?"DEX Screener ↗":"Pump.fun ↗")+"</a></td><td>$"+num(p.entry_price_usd)+"</td><td>$"+num(p.last_price_usd)+"</td><td title='"+esc(ems)+"'>"+esc(cap(p.entry_market_cap_usd,ems))+"</td><td title='"+esc(cms)+"'>"+esc(cap(p.current_market_cap_usd,cms))+"</td><td class='"+(m>=0?"green":"red")+"'>"+(m>=0?"+":"")+m.toFixed(1)+"%</td><td><button class='danger exitOne' data-mint='"+p.mint+"'>Manual exit / retry</button>"+exitState+"</td></tr>"}).join(""):"<tr><td colspan='7' class='empty'>No open positions.</td></tr>";
 document.querySelectorAll(".exitOne").forEach(function(b){b.onclick=function(){if(confirm("Submit manual exit attempt for this position?"))action({action:"manual_exit",mint:b.dataset.mint})}});
 var ts=d.trades||[];el("trades").innerHTML=ts.length?ts.map(function(t){var st=t.strategy==="token_discovery"?"New-token discovery":(t.strategy==="trending_discovery"?"Trending live":"Leader copy");var reconciled=t.action==="RECONCILED_EXIT";var actionLabel=reconciled?"Reconciled exit":t.action;var valueLabel=reconciled?"N/A":"$"+num(t.value_usd);var pnlLabel=reconciled?"N/A":"$"+num(t.pnl_usd);var pnlClass=reconciled?"":(Number(t.pnl_usd)>=0?"green":"red");return "<tr><td>"+String(t.ts||"").replace("T"," ").slice(0,19)+"</td><td>"+st+"</td><td>"+actionLabel+"</td><td class='addr'>"+short(t.mint)+"</td><td>"+valueLabel+"</td><td class='"+pnlClass+"'>"+pnlLabel+"</td><td>"+(t.reason||"")+"</td></tr>"}).join(""):"<tr><td colspan='7' class='empty'>No trades recorded.</td></tr>";
 el("copyTop").disabled=Boolean(d.copy_block_reason);
 el("copyTop").title=d.copy_block_reason||"";
 el("message").textContent=d.copy_block_reason?"Copying unavailable: "+d.copy_block_reason:(d.message||"Ready");
}
async function refreshState(){try{var r=await fetch("/api/state",{cache:"no-store"});render(await r.json())}catch(e){el("message").textContent="Cannot reach local bot. Is pumpfun_bot.py running?"}}
el("copyTop").onclick=function(){action({action:"copy_top10",window:el("window").value})};
el("stopCopy").onclick=function(){action({action:"stop_copy"})};
el("exitAll").onclick=function(){if(confirm("Attempt to exit all tracked positions?"))action({action:"exit_all"})};
el("refresh").onclick=refreshState;el("window").onchange=refreshState;
refreshState();setInterval(refreshState,2000);
</script></body></html>"""


class DashboardServer:
    def __init__(self, port: int, state_provider, command_queue: Queue):
        self.port = port
        self.state_provider = state_provider
        self.command_queue = command_queue
        self.control_token = secrets.token_urlsafe(32)
        self.httpd = None
        self.thread = None

    def start(self):
        provider = self.state_provider
        commands = self.command_queue
        token = self.control_token

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                logger.info("dashboard: " + fmt, *args)

            def send_body(self, status, content_type, body):
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/":
                    self.send_body(200, "text/html; charset=utf-8", _PAGE.replace("__CONTROL_TOKEN__", token).encode())
                elif self.path == "/api/state":
                    try:
                        self.send_body(200, "application/json; charset=utf-8",
                                       json.dumps(provider(), default=str).encode())
                    except Exception as exc:
                        self.send_body(500, "application/json", json.dumps({"message": str(exc)}).encode())
                else:
                    self.send_body(404, "text/plain", b"Not found")

            def do_POST(self):
                if self.path != "/api/action":
                    self.send_body(404, "application/json", b'{"message":"Not found"}')
                    return
                origin = self.headers.get("Origin", "")
                allowed = ("http://127.0.0.1:" + str(self.server.server_port),
                           "http://localhost:" + str(self.server.server_port))
                if origin and origin not in allowed:
                    self.send_body(403, "application/json", b'{"message":"Origin rejected"}')
                    return
                if self.headers.get("X-Control-Token") != token:
                    self.send_body(403, "application/json", b'{"message":"Control token rejected"}')
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                    if length < 2 or length > 4096:
                        raise ValueError("Invalid request size")
                    command = json.loads(self.rfile.read(length))
                    if not isinstance(command, dict) or command.get("action") not in {
                        "copy_top10", "stop_copy", "manual_exit", "exit_all"
                    }:
                        raise ValueError("Unknown action")
                    commands.put(command)
                    self.send_body(202, "application/json; charset=utf-8",
                                   json.dumps({"message":"Command queued; check status and trade log."}).encode())
                except Exception as exc:
                    self.send_body(400, "application/json; charset=utf-8",
                                   json.dumps({"message": str(exc)}).encode())

        try:
            self.httpd = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
            self.httpd.daemon_threads = True
            self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True, name="local-dashboard")
            self.thread.start()
            logger.warning("Local control dashboard: http://127.0.0.1:%s", self.port)
        except OSError as exc:
            logger.error("Could not start local dashboard on port %s: %s", self.port, exc)

    def stop(self):
        if self.httpd:
            self.httpd.shutdown()
            self.httpd.server_close()
