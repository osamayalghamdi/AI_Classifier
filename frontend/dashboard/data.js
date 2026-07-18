/* ── Mock data ─────────────────────────────────────────────────────────
   Mirrors the real API shape from /api/reports/{period} + /incidents so
   switching to live mode is a drop-in replacement.
   Users/teams model the simulator's 8 assign groups mapped to 3 teams.
──────────────────────────────────────────────────────────────────────── */

const TEAMS = {
  "App Support":    ["Ahmed K.", "Sara M.", "Layla R."],
  "Payments":       ["Omar T.", "Noura H."],
  "Infrastructure": ["Khalid B.", "Reem S.", "Yusuf A."],
  "Operations":     ["Faisal A.", "Mona S."],
};

const CURRENT_USER = "Ahmed K.";

function rng(seed) { let s = seed; return () => (s = (s * 16807) % 2147483647) / 2147483647; }

function makeMockData() {
  const r = rng(42);
  const pick = (a) => a[Math.floor(r() * a.length)];
  let idc = 4700;
  const nid = () => `INC-${idc += Math.floor(r() * 5) + 1}`;

  const C = [
    // ── Critical clusters ──
    ["Login authentication timeout", "Auth Service", "Authentication", "Critical",
     "Login timeout: SMS/email codes arriving 10+ minutes late",
     ["Login code arrives too late",
      "Verification token expired before submission",
      "SMS code delayed — can't complete order",
      "Code expires after session times out",
      "OTP 10 min late, checkout failed",
      "Delayed verification code since morning",
      "Login code arrives expired"]],

    ["Payment checkout failures", "Payment Gateway", "Checkout", "Critical",
     "Payment checkout: charged but order stuck pending",
     ["Charged twice, no confirmation",
      "Payment successful but order pending",
      "Money deducted, order confirmation pending",
      "Card charged, app shows failed",
      "Payment deducted but no receipt issued"]],

    ["Account verification pending", "Account Service", "Verification", "Critical",
     "Account verification: approved verification not reflecting in system",
     ["Verification approved but not visible in app",
      "Account verified but system rejects login",
      "ID verified — not showing in profile",
      "Verification status stuck at processing"]],

    ["User role assignment error", "Admin Panel", "Access Control", "Critical",
     "Access control: users assigned to wrong permission groups",
     ["Role changed without notice",
      "Permission group ID incorrect",
      "Users split across different access levels",
      "Wrong role — no access to needed features"]],

    // ── Major clusters ──
    ["App crashes on settings page", "Mobile App", "Settings", "Major",
     "App crash: force-close when opening settings",
     ["App crashes when viewing settings page",
      "App hangs on settings screen",
      "Force close on opening settings"]],

    ["Map markers misplaced", "Maps Service", "Geolocation", "Major",
     "Maps: markers placed 200m off actual location",
     ["Map shows wrong location",
      "Marker at wrong position on map",
      "Geolocation pin misaligned"]],

    ["QR code reader not scanning", "Scanner Service", "Hardware", "Major",
     "Scanner: hardware scanners reject valid QR codes",
     ["QR code won't scan at entry point",
      "Scanner says invalid code but it's active",
      "QR not working at access point"]],

    ["Report export delayed", "Export Service", "Reports", "Major",
     "Export: scheduled report exports not running on time",
     ["Report export delayed over 2 hours",
      "Export missed — no replacement triggered",
      "Export dashboard empty for hours",
      "Export schedule inaccurate"]],

    ["Data import mapping errors", "Data Pipeline", "Import", "Major",
     "Data import: column mapping errors during batch import",
     ["Data mapping failed in import pipeline",
      "Column mismatch in imported file",
      "Import mapping incomplete — data lost"]],

    ["Server health alerts firing", "Infrastructure", "Monitoring", "Major",
     "Monitoring: multiple servers triggering health alerts",
     ["Server health alert at critical threshold",
      "High CPU alerts across cluster",
      "Memory usage spiking — potential OOM risk"]],

    // ── Minor clusters ──
    ["Avatar upload stuck", "Account Service", "Profile", "Minor",
     "File upload: avatar photo rejected at 90% progress",
     ["Photo upload stuck at 90%",
      "Avatar upload keeps failing",
      "Profile picture upload fails"]],

    ["Timezone display incorrect", "Settings Service", "Localization", "Minor",
     "Localization: displayed timezone off by one hour since DST change",
     ["Timezone wrong by one hour",
      "Clock shown incorrect since DST",
      "Time displayed wrong in system"]],

    ["Language preference not saved", "Account Service", "Settings", "Minor",
     "Settings: language preference not persisted across sessions",
     ["Language resets after restart",
      "I set English but it switches back",
      "UI language not remembered"]],

    ["API rate limit exceeded", "API Gateway", "Rate Limiting", "Minor",
     "Rate limiting: API gateway rejecting requests at peak hours",
     ["Rate limit errors at peak times",
      "API requests blocked — too many calls",
      "Rate limit hit, no reset for hours"]],

    ["Database connection pool full", "Database", "Connection Pool", "Minor",
     "Database: connection pool exhausted under load",
     ["Database connections timeout",
      "No connections available in pool",
      "Connection pool full since morning"]],
  ];

  const all = [];
  const clusters = C.map(([name, sys, svc, sev, canon, titles], ci) => {
    // Team mapping with cross-team support
    const primaryTeam = sys === "Payment Gateway" ? "Payments"
      : sys === "Maps Service" || sys === "Scanner Service" || sys === "Export Service"
        || sys === "Data Pipeline" || sys === "Infrastructure" || sys === "API Gateway"
        || sys === "Database" ? "Infrastructure"
      : "App Support";
    const sharedTeams = (name.includes("QR") || name.includes("Map")) ? ["Infrastructure", "App Support"]
      : name === "API rate limit exceeded" || name === "Database connection pool full" ? ["Infrastructure", "App Support"]
      : null;

    const incidents = titles.map((t, i) => {
      const isAr = /[\u0600-\u06FF]/.test(t);
      const mine = primaryTeam === "App Support" && r() < 0.55;
      let assignee = mine ? CURRENT_USER : pick(TEAMS[primaryTeam] || ["Unassigned"]);
      // Some cross-team tickets assigned to the secondary team
      if (sharedTeams && i > Math.floor(titles.length / 2)) {
        assignee = pick(TEAMS[sharedTeams[1]] || ["Unassigned"]);
      }
      return {
        id: nid(), title: t, lang: isAr ? "ar" : "en", severity: sev,
        assignee, assign_group: primaryTeam, team: primaryTeam, system: sys, service: svc,
        canonical_statement: canon,
        similarity_pct: Math.round((0.72 + r() * 0.24) * 1000) / 10,
        created_hours_ago: i === 0 ? Math.round(r() * 2)  // first ticket fresh
          : i < 3 ? Math.round(3 + r() * 12)               // burst cluster
          : Math.round(16 + r() * 56),                      // staggered
        status: i % 5 === 0 ? "resolved"
          : i % 7 === 0 ? "escalated"
          : i % 9 === 0 ? "pending"
          : i % 11 === 0 ? "third_party"
          : i % 13 === 0 ? "verify"
          : i < 2 ? "active"
          : i % 4 === 0 ? "escalated"
          : "active",
        growth: i === 0 && sev !== "Minor" ? `+${Math.floor(r() * 6) + 2} today` : null,
      };
    });
    all.push(...incidents);
    return {
      cluster_id: "G-" + (100 + ci), name, affected_system: sys, affected_service: svc,
      worst_severity: sev, count: incidents.length,
      shared_with_teams: sharedTeams || null,
      summary: `${incidents.length} reports of the same underlying issue across ${sys} / ${svc}. Recommended: single root-cause fix + bulk response.`,
      incidents,
    };
  });

  // Create one mega-cluster (18+ tickets) to test large-group rendering
  const megaTitles = [
    "Customer portal down since morning",
    "Can't log in to customer portal",
    "Login page white screen",
    "500 error on login submit",
    "Portal crashes after form fill",
    "Server timeout on login",
    "Login page won't load",
    "Portal freezes on payment step",
    "Document upload fails during registration",
    "Login page keeps failing after DST",
    "System timeout during login",
    "Error 503 during login step 3",
    "Login fails on mobile browser",
    "Portal very slow during login",
    "Confirmation not showing after registration",
    "CAPTCHA not showing on login",
    "Registration incomplete — no confirmation email",
    "After registration, login still fails",
  ];

  const megaIncidents = megaTitles.map((t, i) => {
    const isAr = /[\u0600-\u06FF]/.test(t);
    const team = i < 15 ? "App Support" : "Infrastructure";
    return {
      id: nid(), title: t, lang: isAr ? "ar" : "en",
      severity: "Critical", assignee: pick(TEAMS[team]),
      assign_group: team, team, system: "Web Portal", service: "Login",
      canonical_statement: "Customer portal: unable to log in or register across web and mobile — likely server-side",
      similarity_pct: Math.round((0.70 + r() * 0.24) * 1000) / 10,
      created_hours_ago: Math.round(i < 10 ? r() * 4 : 6 + r() * 42),
      status: i % 5 === 0 ? "resolved" : i % 3 === 0 ? "escalated" : i % 7 === 0 ? "pending" : i % 11 === 0 ? "third_party" : "active",
      growth: i < 3 ? `+${Math.floor(r() * 8) + 5} today` : null,
    };
  });
  all.push(...megaIncidents);

  clusters.push({
    cluster_id: "G-200", name: "Customer portal unreachable",
    affected_system: "Web Portal", affected_service: "Login",
    worst_severity: "Critical", count: megaIncidents.length,
    shared_with_teams: ["App Support", "Infrastructure"],
    summary: `${megaIncidents.length} tickets — widespread login/registration failure across web and mobile. Multiple teams affected. Probable server-side root cause.`,
    incidents: megaIncidents,
  });

  // ── Individuals (ungrouped) ──
  const indivTitles = [
    ["App slow only on office wifi", "en", "Minor"],
    ["Refund request for cancelled subscription", "en", "Major"],
    ["App notifications too frequent", "en", "Cosmetic"],
    ["Calendar not syncing with Google", "en", "Major"],
    ["Elevator at parking B not working", "en", "Major"],
    ["Lost wallet found — hand to lost & found", "en", "Minor"],
    ["WiFi password in cafeteria not working", "en", "Cosmetic"],
    ["Wheelchair availability at entrance B", "en", "Minor"],
    ["EV charging station full", "en", "Cosmetic"],
    ["Cannot change phone number in profile", "en", "Minor"],
  ];
  const individuals = indivTitles.map(([t, lang, sev]) => {
    const teamsList = Object.keys(TEAMS);
    const team = pick(teamsList);
    const mine = team === "App Support" && r() < 0.5;
    return {
      id: nid(), title: t, lang, severity: sev,
      assignee: mine ? CURRENT_USER : pick(TEAMS[team] || ["Unassigned"]),
      assign_group: team, team, system: team === "Payments" ? "Payment Gateway"
        : team === "Infrastructure" ? "Web Portal" : "General",
      service: "General", status: "active",
      created_hours_ago: Math.round(r() * 72),
    };
  });
  all.push(...individuals);

  return {
    clusters, individuals, all,
    totals: {
      tickets: all.length,
      problems: clusters.length,
      individuals: individuals.length,
    },
  };
}

const MOCK = makeMockData();

/* Weekly history for manager view */
const MOCK_HISTORY = {
  weeks: ["W27", "W28", "W29", "W30"],
  recurring: [
    { name: "Login authentication timeout",    system: "Auth Service / Auth",               counts: [14, 19, 26, 61],  weeks_hit: 4 },
    { name: "Payment checkout failures",       system: "Gateway / Checkout",                 counts: [3, 8, 21, 34],    weeks_hit: 3 },
    { name: "QR code reader failures",         system: "Scanner Service / Hardware",         counts: [0, 5, 11, 18],    weeks_hit: 3 },
    { name: "Map markers misplaced",           system: "Maps Service / Geolocation",         counts: [2, 2, 3, 9],      weeks_hit: 4 },
    { name: "Timezone display incorrect",      system: "Settings Service / Localization",    counts: [0, 0, 4, 7],      weeks_hit: 2 },
    { name: "Customer portal unreachable",     system: "Web Portal / Login",                 counts: [0, 0, 2, 26],     weeks_hit: 2 },
    { name: "Report export delayed",           system: "Export Service / Reports",           counts: [0, 0, 3, 11],     weeks_hit: 2 },
    { name: "User role assignment errors",     system: "Admin Panel / Access Control",       counts: [1, 0, 4, 6],      weeks_hit: 3 },
  ],
  systems: [
    { name: "Auth Service",       count: 124, delta: "+38%" },
    { name: "Payment Gateway",    count: 41,  delta: "+96%" },
    { name: "Scanner Service",    count: 24,  delta: "+9%" },
    { name: "Maps Service",       count: 15,  delta: "-6%" },
    { name: "Export Service",     count: 12,  delta: "+200%" },
    { name: "Account Service",    count: 10,  delta: "+150%" },
    { name: "Infrastructure",     count: 5,   delta: "+67%" },
    { name: "Settings Service",   count: 9,   delta: "+12%" },
    { name: "Data Pipeline",      count: 5,   delta: "+25%" },
    { name: "Web Portal",         count: 18,  delta: "+44%" },
  ],
  resolution: [
    { bucket: "< 4h", pct: 31 }, { bucket: "4–12h", pct: 27 },
    { bucket: "12–24h", pct: 22 }, { bucket: "1–3d", pct: 14 }, { bucket: "> 3d", pct: 6 },
  ],
  deflection: { tickets_closed: 340, responses_sent: 41, ratio: "8.3×" },
  kpis: {
    open: { val: 187, delta: "+12% vs last wk", dir: "up" },
    clusters: { val: 23, delta: "6 critical", dir: "up" },
    mttr: { val: "14.2h", delta: "-2.1h vs last wk", dir: "down" },
    deflection: { val: "8.3×", delta: "340 tickets / 41 replies", dir: "down" },
  },
};
