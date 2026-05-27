"""
Always-On Ops Agent — Dashboard Edition
Triages open incidents, writes a polished HTML dashboard to GitHub Pages,
and maintains a run history log.
"""

import json
import os
import subprocess
import uuid
from datetime import datetime, timezone
from pathlib import Path
import anthropic

client = anthropic.Anthropic()
REPO = Path(__file__).parent
RUN_LOG = REPO / "agent_runs.json"

SYSTEM_PROMPT = """You are an expert on-call ops agent. Triage this incident concisely.

Output using EXACTLY this format (no deviations):

SEVERITY: P0|P1|P2|P3
HYPOTHESIS: One sentence root cause.
EVIDENCE:
- evidence point 1
- evidence point 2
- evidence point 3
ACTION: One concrete recommended action — name the service, file, command, or person.
CONFIDENCE: High|Medium|Low"""

SEV_BADGE  = {"P0": "#dc2626", "P1": "#ea580c", "P2": "#d97706", "P3": "#16a34a"}
SEV_CLASS  = {"P0": "card-p0", "P1": "card-p1", "P2": "card-p2", "P3": "card-p3"}


def load_json(path):
    return json.loads(Path(path).read_text())

def load_issues():
    return [load_json(f) for f in sorted((REPO / "issues").glob("*.json"))]

def load_deploys():
    return load_json(REPO / "deploys" / "recent.json")

def load_runbooks():
    return {f.stem: f.read_text() for f in (REPO / "runbooks").glob("*.md")}

def load_run_log():
    if RUN_LOG.exists():
        return json.loads(RUN_LOG.read_text())
    return {"runs": []}

def save_run_log(log):
    RUN_LOG.write_text(json.dumps(log, indent=2))


def triage_issue(issue, deploys, runbooks):
    runbook_text = "\n\n---\n\n".join(f"# {k}\n{v}" for k, v in runbooks.items())
    prompt = f"""Incident:\n{json.dumps(issue, indent=2)}

Recent deploys:\n{json.dumps(deploys, indent=2)}

Runbooks:\n{runbook_text}

Triage this incident now."""

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=600,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    )
    return parse_triage(response.content[0].text)


def parse_triage(text):
    result = {"severity": "P3", "hypothesis": "", "evidence": [], "action": "", "confidence": "Medium"}
    current = None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("SEVERITY:"):
            for p in ["P0", "P1", "P2", "P3"]:
                if p in line:
                    result["severity"] = p
        elif line.startswith("HYPOTHESIS:"):
            result["hypothesis"] = line.replace("HYPOTHESIS:", "").strip()
        elif line.startswith("EVIDENCE:"):
            current = "evidence"
        elif line.startswith("ACTION:"):
            current = None
            result["action"] = line.replace("ACTION:", "").strip()
        elif line.startswith("CONFIDENCE:"):
            current = None
            for c in ["High", "Medium", "Low"]:
                if c.lower() in line.lower():
                    result["confidence"] = c
        elif current == "evidence" and line.startswith("- "):
            result["evidence"].append(line[2:])
    return result


def time_ago(iso_str):
    if not iso_str:
        return "unknown"
    dt = datetime.fromisoformat(iso_str.replace("Z", "+00:00"))
    delta = datetime.now(timezone.utc) - dt
    mins = int(delta.total_seconds() / 60)
    if mins < 60:
        return f"{mins}m ago"
    hours = mins // 60
    if hours < 24:
        return f"{hours}h ago"
    return f"{hours // 24}d ago"


def generate_dashboard(open_results, resolved_issues, deploys, run_log, run_time):
    now_utc = run_time

    # Stats
    counts = {"P0": 0, "P1": 0, "P2": 0, "P3": 0}
    for r in open_results:
        counts[r["severity"]] = counts.get(r["severity"], 0) + 1
    critical = counts["P0"] + counts["P1"]
    resolved_today = len([i for i in resolved_issues
                          if i.get("resolved_at", "").startswith(now_utc[:7])])

    # Sort: P0 first
    order = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
    sorted_results = sorted(open_results, key=lambda r: order.get(r["severity"], 9))

    # Executive summary — top priority action
    priority_action = ""
    priority_issue_id = ""
    for r in sorted_results:
        if r["severity"] in ("P0", "P1"):
            priority_action = r["action"]
            priority_issue_id = r["issue_id"]
            break

    exec_html = f"""
<div class="exec">
  <div class="exec-tag">
    <div class="exec-tag-title">Executive Summary</div>
    <div class="exec-tag-sub">As of {now_utc} UTC</div>
  </div>
  <div class="exec-metric">
    <div class="exec-val red">{critical} critical incident{"s" if critical != 1 else ""} open</div>
    <div class="exec-key">Requires immediate engineering action</div>
  </div>
  <div class="exec-metric">
    <div class="exec-val amber">{len(open_results)} total open incidents</div>
    <div class="exec-key">{counts.get("P2", 0)} warning · {counts.get("P3", 0)} low priority</div>
  </div>
  <div class="exec-metric">
    <div class="exec-val blue">{priority_issue_id or "No critical issues"}</div>
    <div class="exec-key">Highest priority incident</div>
  </div>
</div>"""

    # Alert banner
    critical_ids = [r["issue_id"] for r in sorted_results if r["severity"] in ("P0", "P1")]
    alert_html = ""
    if critical_ids:
        ids_str = " · ".join(critical_ids)
        alert_html = f"""
<div class="alert">
  <span class="alert-dot"></span>
  <strong>{len(critical_ids)} critical issue(s) require immediate action</strong>
  <span class="alert-ids">{ids_str}</span>
</div>"""

    # Priority action bar
    action_bar_html = ""
    if priority_action:
        action_bar_html = f"""
<div class="action-bar">
  <span class="action-bar-lbl">▶ Priority Action</span>
  <span class="action-bar-val">{priority_action}</span>
</div>"""

    # Incident cards
    cards_html = ""
    for r in sorted_results:
        sev = r["severity"]
        evidence_items = "".join(f"<li>{e}</li>" for e in r["evidence"])
        conf_icon = {"High": "✅", "Medium": "⚠️", "Low": "❓"}.get(r["confidence"], "")
        cards_html += f"""
    <div class="incident-card {SEV_CLASS.get(sev, '')}">
      <div class="card-header">
        <span class="sev-badge" style="background:{SEV_BADGE[sev]}">{sev}</span>
        <span class="issue-id">{r["issue_id"]}</span>
        <span class="issue-title">{r["title"]}</span>
        <span class="conf-badge">{conf_icon} {r["confidence"]} confidence</span>
      </div>
      <div class="card-body">
        <div><div class="label">Root Cause</div><div class="value">{r["hypothesis"]}</div></div>
        <div><div class="label">Evidence</div><ul class="evidence-list">{evidence_items}</ul></div>
        <div class="card-action">
          <span class="action-label">▶ ACTION</span>
          <span class="action-text">{r["action"]}</span>
        </div>
      </div>
    </div>"""

    # Deploy timeline
    deploy_items = ""
    for d in deploys.get("deploys", [])[:6]:
        svc = d.get("service", "")
        ver = d.get("version", "")
        ago = time_ago(d.get("deployed_at", ""))
        summary = d.get("summary", "")
        lkg = d.get("last_known_good", "")
        rollback = f"✓ rollback available → {lkg}" if d.get("rollback_available") else "✗ no rollback available"
        rb_color = "#16a34a" if d.get("rollback_available") else "#dc2626"
        deploy_items += f"""
      <div class="deploy-item">
        <div class="deploy-header">
          <span class="deploy-svc">{svc}</span>
          <span class="deploy-ver">{ver}</span>
          <span class="deploy-ago">{ago}</span>
        </div>
        <div class="deploy-summary">{summary}</div>
        <div class="deploy-rollback" style="color:{rb_color}">{rollback}</div>
      </div>"""

    # Resolved incidents
    resolved_html = ""
    for i in resolved_issues[-4:][::-1]:
        resolved_html += f"""
      <div class="resolved-item">
        <span class="resolved-id">{i["id"]}</span>
        <span class="resolved-title">{i.get("title", "")[:48]}</span>
        <span class="resolved-age">{time_ago(i.get("resolved_at", ""))}</span>
      </div>"""

    # Run history
    runs_html = ""
    for run in run_log.get("runs", [])[-6:][::-1]:
        t = run.get("time", "")[:16].replace("T", " ")
        critical_count = run.get("critical_count", 0)
        open_count = run.get("open_count", 0)
        dot_color = "#dc2626" if critical_count > 0 else "#16a34a"
        runs_html += f"""
      <div class="run-item">
        <span class="run-dot" style="background:{dot_color}"></span>
        <span class="run-time">{t} UTC</span>
        <span class="run-detail">{open_count} open · {critical_count} critical</span>
      </div>"""

    resolved_fallback = '<div style="color:#94a3b8;font-size:13px;">No resolved incidents</div>'
    runs_fallback     = '<div style="color:#94a3b8;font-size:13px;">No runs yet</div>'

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <meta http-equiv="refresh" content="60">
  <title>Ops Agent Dashboard</title>
  <style>
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
           background: #f1f5f9; color: #0f172a; min-height: 100vh; }}

    /* ── Header ── */
    .header {{ background: #1e293b; padding: 14px 28px;
              display: flex; align-items: center; justify-content: space-between; }}
    .header-left {{ display: flex; align-items: center; gap: 12px; }}
    .header-title {{ font-size: 17px; font-weight: 700; color: #f8fafc; }}
    .live-pill {{ display: flex; align-items: center; gap: 6px; background: #052e16;
                 border: 1px solid #166534; color: #4ade80; padding: 3px 12px;
                 border-radius: 20px; font-size: 11px; font-weight: 700; }}
    .live-dot {{ width: 7px; height: 7px; background: #4ade80; border-radius: 50%;
                animation: blink 1.4s ease-in-out infinite; }}
    @keyframes blink {{ 0%,100%{{opacity:1}} 50%{{opacity:.25}} }}
    .header-right {{ display: flex; align-items: center; gap: 16px; }}
    .header-meta {{ font-size: 12px; color: #94a3b8; text-align: right; line-height: 1.8; }}
    .header-meta strong {{ color: #cbd5e1; }}
    .powered {{ font-size: 11px; background: #0f172a; border: 1px solid #334155;
               color: #64748b; padding: 4px 12px; border-radius: 6px; }}
    .powered span {{ color: #a5b4fc; font-weight: 600; }}

    /* ── Stats bar ── */
    .stats-bar {{ display: grid; grid-template-columns: repeat(5,1fr);
                 border-bottom: 2px solid #e2e8f0; }}
    .stat {{ background: #ffffff; padding: 18px 20px; text-align: center;
            border-right: 1px solid #e2e8f0; position: relative; }}
    .stat:last-child {{ border-right: none; }}
    .stat-num {{ font-size: 32px; font-weight: 800; line-height: 1; color: #0f172a; }}
    .stat-label {{ font-size: 10px; color: #64748b; margin-top: 4px;
                  text-transform: uppercase; letter-spacing: .07em; font-weight: 600; }}

    /* Pulsing red glow on critical tile */
    @keyframes critical-glow {{
      0%,100% {{ box-shadow: inset 0 -3px 0 #dc2626, 0 0 0 0 rgba(220,38,38,0); }}
      50%      {{ box-shadow: inset 0 -3px 0 #dc2626, 0 4px 24px 0 rgba(220,38,38,.22); }}
    }}
    .stat.critical {{ animation: critical-glow 2s ease-in-out infinite; border-bottom: 3px solid #dc2626; }}
    .stat.critical .stat-num {{ color: #dc2626; }}
    .stat.warning  .stat-num {{ color: #ea580c; }}
    .stat.low      .stat-num {{ color: #16a34a; }}
    .stat.total    .stat-num {{ color: #0f172a; }}
    .stat.resolved .stat-num {{ color: #2563eb; }}

    /* ── Executive Summary ── */
    .exec {{ background: #fff; border-bottom: 2px solid #e2e8f0;
            padding: 16px 28px; display: grid;
            grid-template-columns: 140px 1fr 1fr 1fr; gap: 0; }}
    .exec-tag {{ display: flex; flex-direction: column; justify-content: center;
                padding-right: 20px; border-right: 1px solid #e2e8f0; }}
    .exec-tag-title {{ font-size: 10px; font-weight: 700; color: #2563eb;
                      text-transform: uppercase; letter-spacing: .1em; }}
    .exec-tag-sub {{ font-size: 11px; color: #94a3b8; margin-top: 2px; }}
    .exec-metric {{ padding: 0 20px; border-right: 1px solid #e2e8f0;
                   display: flex; flex-direction: column; justify-content: center; }}
    .exec-metric:last-child {{ border-right: none; }}
    .exec-val {{ font-size: 14px; font-weight: 700; color: #0f172a; line-height: 1.4; }}
    .exec-val.red   {{ color: #dc2626; }}
    .exec-val.amber {{ color: #ea580c; }}
    .exec-val.blue  {{ color: #2563eb; }}
    .exec-key {{ font-size: 11px; color: #64748b; margin-top: 2px; }}

    /* ── Alert banner ── */
    .alert {{ background: #fef2f2; border-bottom: 2px solid #fecaca;
             padding: 11px 28px; display: flex; align-items: center;
             gap: 12px; font-size: 13px; color: #991b1b; font-weight: 600; }}
    .alert-dot {{ width: 9px; height: 9px; background: #dc2626; border-radius: 50%;
                 animation: blink .9s ease-in-out infinite; flex-shrink: 0; }}
    .alert-ids {{ margin-left: auto; font-family: monospace; font-size: 12px;
                 color: #dc2626; font-weight: 700; }}

    /* ── Priority action bar ── */
    .action-bar {{ background: #eff6ff; border-bottom: 2px solid #bfdbfe;
                  padding: 11px 28px; display: flex; align-items: center; gap: 12px; }}
    .action-bar-lbl {{ font-size: 11px; font-weight: 700; color: #1d4ed8;
                      text-transform: uppercase; letter-spacing: .07em; white-space: nowrap; }}
    .action-bar-val {{ font-size: 13px; color: #1e40af; line-height: 1.5; }}

    /* ── Main layout ── */
    .main {{ display: grid; grid-template-columns: 1fr 300px;
            gap: 20px; padding: 20px 28px; max-width: 1400px; margin: 0 auto; }}

    .section-title {{ font-size: 11px; font-weight: 700; color: #64748b;
                     text-transform: uppercase; letter-spacing: .08em; margin-bottom: 12px; }}

    /* ── Incident cards ── */
    .incident-card {{ background: #ffffff; border-radius: 10px; margin-bottom: 12px;
                     border: 1px solid #e2e8f0; overflow: hidden;
                     box-shadow: 0 1px 3px rgba(0,0,0,.06); }}
    .card-header {{ display: flex; align-items: center; gap: 10px;
                   padding: 12px 16px; flex-wrap: wrap;
                   border-bottom: 1px solid #f1f5f9; }}
    .sev-badge {{ color: #fff; padding: 3px 10px; border-radius: 20px;
                 font-size: 11px; font-weight: 700; flex-shrink: 0; }}
    .issue-id {{ font-family: monospace; font-size: 12px; color: #64748b; font-weight: 600; }}
    .issue-title {{ font-size: 13px; color: #0f172a; font-weight: 600; flex: 1; }}
    .conf-badge {{ font-size: 11px; color: #64748b; flex-shrink: 0; }}
    .card-body {{ padding: 14px 16px; display: flex; flex-direction: column; gap: 10px; }}
    .label {{ font-size: 10px; font-weight: 700; color: #94a3b8;
             text-transform: uppercase; letter-spacing: .07em; margin-bottom: 3px; }}
    .value {{ font-size: 13px; color: #334155; line-height: 1.6; }}
    .evidence-list {{ padding-left: 16px; }}
    .evidence-list li {{ font-size: 12px; color: #475569; line-height: 1.6; margin-bottom: 2px; }}
    .card-action {{ background: #f0f9ff; border: 1px solid #bae6fd; border-radius: 8px;
                   padding: 10px 12px; display: flex; gap: 8px; align-items: flex-start; }}
    .action-label {{ font-size: 10px; font-weight: 700; color: #0369a1;
                    white-space: nowrap; padding-top: 2px; }}
    .action-text {{ font-size: 12px; color: #0c4a6e; line-height: 1.6; }}

    /* Left border accent per severity */
    .card-p0 {{ border-left: 4px solid #dc2626; }}
    .card-p1 {{ border-left: 4px solid #ea580c; }}
    .card-p2 {{ border-left: 4px solid #d97706; }}
    .card-p3 {{ border-left: 4px solid #16a34a; }}

    /* ── Sidebar ── */
    .sidebar {{ display: flex; flex-direction: column; gap: 16px; }}
    .sidebar-card {{ background: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px;
                    padding: 16px; box-shadow: 0 1px 3px rgba(0,0,0,.06); }}

    .deploy-item {{ padding: 10px 0; border-bottom: 1px solid #f1f5f9; }}
    .deploy-item:last-child {{ border-bottom: none; padding-bottom: 0; }}
    .deploy-header {{ display: flex; align-items: center; gap: 8px; margin-bottom: 3px; }}
    .deploy-svc {{ font-size: 12px; font-weight: 700; color: #0f172a; }}
    .deploy-ver {{ font-size: 11px; color: #64748b; font-family: monospace; }}
    .deploy-ago {{ font-size: 11px; color: #94a3b8; margin-left: auto; }}
    .deploy-summary {{ font-size: 11px; color: #64748b; margin-bottom: 3px; }}
    .deploy-rollback {{ font-size: 11px; font-weight: 600; }}

    .resolved-item {{ display: flex; align-items: center; gap: 8px;
                     padding: 7px 0; border-bottom: 1px solid #f1f5f9; }}
    .resolved-item:last-child {{ border-bottom: none; }}
    .resolved-id {{ font-family: monospace; font-size: 11px; color: #16a34a; font-weight: 700; }}
    .resolved-title {{ font-size: 11px; color: #64748b; flex: 1; }}
    .resolved-age {{ font-size: 10px; color: #94a3b8; }}

    .run-item {{ display: flex; align-items: center; gap: 8px;
                padding: 7px 0; border-bottom: 1px solid #f1f5f9; }}
    .run-item:last-child {{ border-bottom: none; }}
    .run-dot {{ width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0; }}
    .run-time {{ font-size: 11px; color: #334155; font-family: monospace; }}
    .run-detail {{ font-size: 11px; color: #94a3b8; margin-left: auto; }}

    .footer {{ text-align: center; padding: 24px; color: #94a3b8; font-size: 12px; }}
  </style>
</head>
<body>

<div class="header">
  <div class="header-left">
    <div class="header-title">🔍 Always-On Ops Agent</div>
    <div class="live-pill"><span class="live-dot"></span> LIVE</div>
  </div>
  <div class="header-right">
    <div class="header-meta">
      Last run: <strong>{now_utc} UTC</strong><br>
      Next run in: <strong id="cd">—</strong> · Auto-refreshes every 60s
    </div>
    <div class="powered">Powered by <span>Claude Sonnet 4.6</span></div>
  </div>
</div>

<div class="stats-bar">
  <div class="stat critical">
    <div class="stat-num">{critical}</div>
    <div class="stat-label">Critical (P0 + P1)</div>
  </div>
  <div class="stat warning">
    <div class="stat-num">{counts.get("P2", 0)}</div>
    <div class="stat-label">Warning (P2)</div>
  </div>
  <div class="stat low">
    <div class="stat-num">{counts.get("P3", 0)}</div>
    <div class="stat-label">Low (P3)</div>
  </div>
  <div class="stat total">
    <div class="stat-num">{len(open_results)}</div>
    <div class="stat-label">Open Incidents</div>
  </div>
  <div class="stat resolved">
    <div class="stat-num">{resolved_today}</div>
    <div class="stat-label">Resolved Today</div>
  </div>
</div>

{exec_html}
{alert_html}
{action_bar_html}

<div class="main">
  <div class="incidents-col">
    <div class="section-title">Open Incidents — sorted by severity</div>
    {cards_html}
  </div>

  <div class="sidebar">
    <div class="sidebar-card">
      <div class="section-title">Recent Deploys</div>
      {deploy_items}
    </div>

    <div class="sidebar-card">
      <div class="section-title">Resolved Recently</div>
      {resolved_html if resolved_html else resolved_fallback}
    </div>

    <div class="sidebar-card">
      <div class="section-title">Agent Run History</div>
      {runs_html if runs_html else runs_fallback}
    </div>
  </div>
</div>

<div class="footer">Always-On Ops Agent · sugoi-star/always-on-agent · Refreshes hourly via Claude Routines</div>

<script>
  function tick() {{
    const now = new Date(), next = new Date(now);
    next.setMinutes(7, 0, 0);
    if (now.getMinutes() >= 7) next.setHours(next.getHours() + 1);
    const s = Math.max(0, Math.floor((next - now) / 1000));
    document.getElementById('cd').textContent =
      String(Math.floor(s / 60)).padStart(2,'0') + ':' + String(s % 60).padStart(2,'0');
  }}
  tick(); setInterval(tick, 1000);
</script>
</body>
</html>"""


def commit_dashboard(html, run_log):
    (REPO / "index.html").write_text(html)
    save_run_log(run_log)
    subprocess.run(["git", "add", "index.html", "agent_runs.json"],
                   cwd=REPO, capture_output=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    subprocess.run(["git", "commit", "-m", f"ops-agent: triage report {ts}"],
                   cwd=REPO, check=True)
    subprocess.run(["git", "push"], cwd=REPO, check=True)
    print("✅ Dashboard pushed → https://sugoi-star.github.io/always-on-agent/")


def run():
    print("🔍 Always-On Ops Agent starting...\n")
    issues   = load_issues()
    deploys  = load_deploys()
    runbooks = load_runbooks()
    run_log  = load_run_log()

    open_issues     = [i for i in issues if i.get("status") == "open"]
    resolved_issues = [i for i in issues if i.get("status") == "resolved"]
    print(f"Found {len(open_issues)} open, {len(resolved_issues)} resolved.\n{'='*60}\n")

    results = []
    for issue in open_issues:
        print(f"Triaging {issue['id']}...")
        triage = triage_issue(issue, deploys, runbooks)
        triage["issue_id"] = issue["id"]
        triage["title"]    = issue.get("title", "")
        results.append(triage)
        print(f"  → {triage['severity']} ({triage['confidence']} confidence)")

    # Update run log
    critical_count = sum(1 for r in results if r["severity"] in ("P0", "P1"))
    run_log["runs"].append({
        "time": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "open_count": len(open_issues),
        "critical_count": critical_count,
    })
    run_log["runs"] = run_log["runs"][-20:]  # keep last 20

    run_time = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    html = generate_dashboard(results, resolved_issues, deploys, run_log, run_time)
    print("\nPushing dashboard...")
    commit_dashboard(html, run_log)


if __name__ == "__main__":
    run()
