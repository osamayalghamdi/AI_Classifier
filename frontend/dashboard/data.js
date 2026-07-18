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
    ["OTP delivery delayed", "Nusuk App", "Authentication", "Critical",
     "OTP delivery: SMS codes arriving 10+ minutes late",
     ["رمز التحقق يتأخر كثيراً", "OTP arrives too late to login",
      "رسالة الكود توصل بعد ١٥ دقيقة", "Verification code expired before arrival",
      "ما وصلني رمز التحقق نهائياً", "SMS code delayed — can't book",
      "تأخير رسائل OTP منذ الصباح", "Code comes after session times out",
      "الرمز يصل متأخر وما ينفع", "OTP 10 min late, booking failed",
      "رمز الدخول ما يوصل إلا متأخر", "Delayed OTP since morning",
      "كود التفعيل يتأخر", "Login code arrives expired"]],

    ["Payment charged, booking not confirmed", "Payment Gateway", "Checkout", "Critical",
     "Payment checkout: charged but booking stuck pending",
     ["خصم المبلغ وما تأكد الحجز", "Charged twice, no confirmation",
      "الدفع نجح بس الحجز معلق", "Money deducted, booking pending",
      "اتخصم مني مرتين", "Payment success but no permit issued",
      "حجزي معلق بعد الدفع", "Card charged, app shows failed",
      "مبلغ التأشيرة انخصم وما صدر", "Visa fee deducted, no visa issued"]],

    ["Visa approval stuck for 48h", "Visa Services", "Permits", "Critical",
     "Visa issuance: approved permits not reflecting in system",
     ["التأشيرة معلقة من يومين", "Visa approved but not visible in app",
      "الفيزا صدرت وما ظهرت بحسابي", "Permit shows issued but gate rejects",
      "تصريح العمرة ما يظهر بعد الدفع", "Visa status stuck at processing"]],

    ["Mina tent assignment wrong", "Accommodation", "Mina", "Critical",
     "Accommodation: pilgrims assigned to wrong tent blocks in Mina",
     ["خيمة غير اللي مخصص لي", "Tent assignment changed without notice",
      "رقم الخيمة اللي عندي غير صحيح", "Family split across different blocks",
      "أسرتي مو معي في نفس الخيمة", "Wrong tent zone — far from group"]],

    // ── Major clusters ──
    ["App crashes on permit screen", "Nusuk App", "Permits", "Major",
     "App crash: force-close when opening permit details",
     ["التطبيق يطلعني لما أفتح التصريح", "Crash when viewing my permit",
      "التطبيق يعلق عند شاشة التصاريح", "App closes itself on permit page",
      "ما أقدر أفتح تصريحي، يكرش", "Force close on permit details"]],

    ["Map shows wrong gate location", "Maps Service", "Navigation", "Major",
     "Maps: gate markers placed 200m off actual location",
     ["الخريطة توديني مكان غلط", "Gate 79 shown at wrong location",
      "الموقع في الخريطة غير صحيح", "Navigation sends us to closed gate",
      "خريطة الحرم غلط عند باب الملك"]],

    ["Permit QR not scanning at gates", "Nusuk App", "Permits", "Major",
     "Permit QR: gate scanners reject valid codes",
     ["الباركود ما يقبل عند البوابة", "QR code won't scan at King Fahd gate",
      "رجل الأمن قال الكود غير صالح", "Scanner says invalid permit but it's active",
      "كيو أر ما يشتغل في الدخول"]],

    ["Bus to Arafat delayed 2+ hours", "Transportation", "Buses", "Major",
     "Transport: Arafat shuttle buses not running on schedule",
     ["الباص تأخر أكثر من ساعتين", "Arafat bus missed — no replacement",
      "ما في باصات من المخيم للحرم", "Bus stop empty for hours",
      "جدول الباصات غير دقيق"]],

    ["Lost luggage at Makkah hotel", "Lost & Found", "Hotel Services", "Major",
     "Lost property: luggage left at hotel lobby not recovered",
     ["شنتي ضاعت في الفندق", "Suitcase missing from hotel room",
      "الشنطة راحت مع باص غير باصنا", "Bag taken by wrong bus",
      "أغراضي ضايعة من الاستقبال"]],

    ["Heat exhaustion cases near Jamarat", "Health Services", "Emergency", "Major",
     "Health: multiple pilgrims needing cooling stations near Jamarat bridge",
     ["في حالات إعياء حراري عند الجمرات", "Pilgrim collapsed near pillar 2",
      "ناس واقعة من الحر قرب الجمرات", "Need water misters at Jamarat area",
      "حالة إغماء بسبب الحر الشديد"]],

    // ── Minor clusters ──
    ["Profile photo upload fails", "Nusuk App", "Profile", "Minor",
     "File upload: avatar photo rejected at 90% progress",
     ["الصورة ما ترفع", "Photo upload stuck at 90%",
      "ما يقبل صورتي الشخصية", "Avatar upload keeps failing"]],

    ["Prayer times off by one hour", "Content Service", "Prayer Times", "Minor",
     "Prayer times: displayed one hour ahead since DST change",
     ["أوقات الصلاة غلط بساعة", "Prayer times wrong since DST",
      "الفجر ظاهر الساعة ٤ و هو ٣", "Maghrib shown wrong time"]],

    ["App language resets to Arabic", "Nusuk App", "Settings", "Minor",
     "Settings: language preference not persisted across sessions",
     ["اللغة ترجع عربي كل مرة", "Language resets after app restart",
      "I set English but it switches back"]],

    ["Water station at Jamarat empty", "Services", "Water", "Minor",
     "Facilities: drinking water dispenser dry at peak hours",
     ["ما في ماء عند الجمرات", "Water cooler empty on level 2",
      "موية الشرب انتهت من الصباح", "No refill for 6 hours"]],

    ["Shower water cold at Mina camp", "Accommodation", "Mina Facilities", "Minor",
     "Facilities: showers only running cold water",
     ["الماء بارد في دورات المياه", "No hot water in Mina tent",
      "ما في ماء حار للاستحمام", "Cold shower since morning"]],
  ];

  const all = [];
  const clusters = C.map(([name, sys, svc, sev, canon, titles], ci) => {
    // Team mapping with cross-team support
    const primaryTeam = sys === "Payment Gateway" || sys === "Visa Services" ? "Payments"
      : sys === "Transportation" ? "Operations"
      : sys === "Lost & Found" || sys === "Accommodation" ? "Operations"
      : sys === "Health Services" || sys === "Services" ? "Operations"
      : sys === "Maps Service" ? "Infrastructure"
      : "App Support";
    const sharedTeams = (name.includes("QR") || name.includes("gate")) ? ["App Support", "Infrastructure"]
      : name.includes("Water") || name.includes("Shower") ? ["Operations", "App Support"]
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

  // Create one mega-cluster (25+ tickets) to test large-group rendering
  const megaTitles = [
    "منصة التسجيل معلقة", "Registration portal down since morning",
    "ما أقدر أسجل في نُسك", "Can't log in to Nusuk portal",
    "بوابة التسجيل لا تعمل", "Registration page white screen",
    "خطأ ٥٠٠ عند الدخول للتسجيل", "500 error on registration submit",
    "التسجيل يقفل بعد إدخال البيانات", "Portal crashes after form fill",
    "ما في رد من الخادم", "Server timeout on register",
    "صفحة التسجيل ما تفتح أبداً", "Registration page won't load",
    "التقديم على عمرة معلق", "Umrah application stuck",
    "الموقع يعلق عند الدفع للتسجيل", "Portal freezes on payment step",
    "فشل رفع المستندات للتسجيل", "Document upload fails during reg",
    "registration page keeps failing after DST", 
    "رمز التحقق ما يوصل لتسجيل الدخول",
    "ما أقدر أكمل التسجيل بعد إدخال بياناتي",
    "التسجيل يعلق بعد خطوة تأكيد البريد",
    "System timeout during registration",
    "الموقع يقول خدمة غير متوفرة",
    "Error 503 during registration step 3",
    "Registration fails on mobile browser",
    "ما في خيار للجنسية في القائمة",
    "الموقع بطيء جداً في التسجيل",
    "تأكيد الحجز ما يظهر بعد التسجيل",
    "CAPTCHA not showing on registration",
    "ما أقدر أختار الدولة من القائمة",
    "صفحة التسجيل توديني على صفحة خطأ",
    "Registration incomplete — no confirmation email",
    "بعد التسجيل ما يقبل الدخول",
  ];

  const megaIncidents = megaTitles.map((t, i) => {
    const isAr = /[\u0600-\u06FF]/.test(t);
    const team = i < 15 ? "App Support" : "Infrastructure";
    return {
      id: nid(), title: t, lang: isAr ? "ar" : "en",
      severity: "Critical", assignee: pick(TEAMS[team]),
      assign_group: team, team, system: "Nusuk App", service: "Registration",
      canonical_statement: "Registration portal: unable to submit registration forms across web and mobile — likely server-side",
      similarity_pct: Math.round((0.70 + r() * 0.24) * 1000) / 10,
      created_hours_ago: Math.round(i < 10 ? r() * 4 : 6 + r() * 42),
      status: i % 5 === 0 ? "resolved" : i % 3 === 0 ? "escalated" : i % 7 === 0 ? "pending" : i % 11 === 0 ? "third_party" : "active",
      growth: i < 3 ? `+${Math.floor(r() * 8) + 5} today` : null,
    };
  });
  all.push(...megaIncidents);

  clusters.push({
    cluster_id: "G-200", name: "Registration portal down",
    affected_system: "Nusuk App", affected_service: "Registration",
    worst_severity: "Critical", count: megaIncidents.length,
    shared_with_teams: ["App Support", "Infrastructure"],
    summary: `${megaIncidents.length} tickets — widespread registration failure across web and mobile. Multiple teams affected. Probable server-side root cause.`,
    incidents: megaIncidents,
  });

  // ── Individuals (ungrouped) ──
  const indivTitles = [
    ["App slow only on hotel wifi", "en", "Minor"],
    ["ما أقدر أغير رقم جوالي", "ar", "Minor"],
    ["Refund request for cancelled Mutamerr trip", "en", "Major"],
    ["وشلون أضيف مرافق؟", "ar", "Minor"],
    ["App notifications too frequent", "en", "Cosmetic"],
    ["أبي ألغي حسابي نهائياً", "ar", "Minor"],
    ["Haramain train tickets not syncing", "en", "Major"],
    ["العمرة ما تظهر في سجلّي", "ar", "Minor"],
    ["Elevator at parking B not working", "en", "Major"],
    ["مكيف المخيم ما يشتغل", "ar", "Minor"],
    ["Lost wallet found — hand to lost & found", "en", "Minor"],
    ["WiFi password at Mina camp not working", "en", "Cosmetic"],
    ["جوال ضايع — أحد عثر عليه", "ar", "Minor"],
    ["Wheelchair availability at gate 15", "en", "Minor"],
    ["Electric scooter charging station full", "en", "Cosmetic"],
  ];
  const individuals = indivTitles.map(([t, lang, sev]) => {
    const teamsList = Object.keys(TEAMS);
    const team = pick(teamsList);
    const mine = team === "App Support" && r() < 0.5;
    return {
      id: nid(), title: t, lang, severity: sev,
      assignee: mine ? CURRENT_USER : pick(TEAMS[team] || ["Unassigned"]),
      assign_group: team, team, system: team === "Payments" ? "Payment Gateway"
        : team === "Infrastructure" ? "Nusuk App" : "General",
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
    { name: "OTP delivery delayed",        system: "Nusuk App / Auth",             counts: [14, 19, 26, 61],  weeks_hit: 4 },
    { name: "Payment charged, no booking", system: "Gateway / Checkout",            counts: [3, 8, 21, 34],   weeks_hit: 3 },
    { name: "Permit QR scan failures",     system: "Permits",                       counts: [0, 5, 11, 18],   weeks_hit: 3 },
    { name: "Wrong gate map markers",      system: "Maps",                          counts: [2, 2, 3, 9],     weeks_hit: 4 },
    { name: "Prayer times offset",         system: "Content",                       counts: [0, 0, 4, 7],     weeks_hit: 2 },
    { name: "Registration portal hangs",   system: "Nusuk App / Registration",      counts: [0, 0, 2, 26],     weeks_hit: 2 },
    { name: "Bus delays to holy sites",    system: "Transportation / Buses",        counts: [0, 0, 3, 11],    weeks_hit: 2 },
    { name: "Mina tent assignment errors", system: "Accommodation / Mina",          counts: [1, 0, 4, 6],     weeks_hit: 3 },
  ],
  systems: [
    { name: "Nusuk App",          count: 124, delta: "+38%" },
    { name: "Payment Gateway",    count: 41,  delta: "+96%" },
    { name: "Permits Service",    count: 24,  delta: "+9%" },
    { name: "Maps Service",       count: 15,  delta: "-6%" },
    { name: "Transportation",     count: 12,  delta: "+200%" },
    { name: "Accommodation",      count: 10,  delta: "+150%" },
    { name: "Health Services",    count: 5,   delta: "+67%" },
    { name: "Content Service",    count: 9,   delta: "+12%" },
    { name: "Lost & Found",       count: 5,   delta: "+25%" },
    { name: "Haramain Rail Sync", count: 4,   delta: "0%" },
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
