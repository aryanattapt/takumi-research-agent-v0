import { FormEvent, useEffect, useMemo, useState } from "react";
import { api } from "./api";
import type { Blog, Provider, Run } from "./types";

type Page = "overview" | "blogs" | "providers" | "runs" | "settings";
const fields = ["technology", "physics", "chemistry", "biology"] as const;

function ErrorText({ children }: { children: string }) {
  return <p role="alert" className="mt-3 rounded-xl border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">{children}</p>;
}

function Login({ onLogin }: { onLogin: (csrf: string, username: string) => void }) {
  const [needsSetup, setNeedsSetup] = useState<boolean | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { api<{ needs_admin_setup: boolean }>("/api/v1/setup/status").then((v) => setNeedsSetup(v.needs_admin_setup)).catch((e) => setError(e.message)); }, []);
  async function submit(event: FormEvent) {
    event.preventDefault(); setBusy(true); setError("");
    try {
      if (needsSetup) await api("/api/v1/setup/admin", { method: "POST", body: JSON.stringify({ username, password }) });
      const result = await api<{ username: string; csrf_token: string }>("/api/v1/auth/login", { method: "POST", body: JSON.stringify({ username, password }) });
      onLogin(result.csrf_token, result.username);
    } catch (e) { setError(e instanceof Error ? e.message : "Login gagal"); }
    finally { setBusy(false); }
  }
  return <main className="grid min-h-screen place-items-center bg-ink px-4 py-10">
    <form onSubmit={submit} className="w-full max-w-md rounded-3xl bg-white p-8 shadow-panel">
      <div className="mb-8 flex items-center gap-3"><div className="grid h-12 w-12 place-items-center rounded-2xl bg-teal-50 text-xl font-bold text-teal-600">T</div><div><h1 className="text-xl font-bold text-ink">Takumi</h1><p className="text-sm text-slate-500">Research Agent Dashboard</p></div></div>
      <h2 className="text-lg font-semibold text-ink">{needsSetup ? "Buat administrator" : "Masuk ke dashboard"}</h2>
      <p className="mt-1 text-sm text-slate-500">{needsSetup ? "Akun pertama menjadi satu-satunya administrator." : "Kelola koneksi, jadwal, dan hasil riset."}</p>
      <label className="field-label mt-6">Username<input autoComplete="username" required minLength={3} value={username} onChange={(e) => setUsername(e.target.value)} className="field-input" /></label>
      <label className="field-label mt-4">Password<input type="password" autoComplete={needsSetup ? "new-password" : "current-password"} required minLength={needsSetup ? 12 : 1} value={password} onChange={(e) => setPassword(e.target.value)} className="field-input" /></label>
      {error && <ErrorText>{error}</ErrorText>}
      <button disabled={busy || needsSetup === null} className="primary-button mt-6 w-full">{busy ? "Memproses…" : needsSetup ? "Buat akun dan masuk" : "Masuk"}</button>
    </form>
  </main>;
}

function App() {
  const [csrf, setCsrf] = useState("");
  const [username, setUsername] = useState("");
  const [page, setPage] = useState<Page>("overview");
  const [blogs, setBlogs] = useState<Blog[]>([]);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [runs, setRuns] = useState<Run[]>([]);
  const [control, setControl] = useState({ content_mutations_paused: true, recovery_mode: false, revision: 1 });
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [showBlogForm, setShowBlogForm] = useState(false);
  const [showProviderForm, setShowProviderForm] = useState(false);
  const [editingProvider, setEditingProvider] = useState<Provider | null>(null);

  async function loadAll() {
    setLoading(true); setError("");
    try {
      const [nextBlogs, nextProviders, nextRuns, nextControl] = await Promise.all([
        api<Blog[]>("/api/v1/blogs"), api<Provider[]>("/api/v1/providers"), api<Run[]>("/api/v1/runs"),
        api<typeof control>("/api/v1/control"),
      ]);
      setBlogs(nextBlogs); setProviders(nextProviders); setRuns(nextRuns); setControl(nextControl);
    } catch (e) { setError(e instanceof Error ? e.message : "Gagal memuat dashboard"); }
    finally { setLoading(false); }
  }

  useEffect(() => {
    api<{ username: string; csrf_token: string }>("/api/v1/auth/session")
      .then((session) => { setUsername(session.username); setCsrf(session.csrf_token); })
      .catch(() => setLoading(false));
  }, []);
  useEffect(() => { if (csrf) void loadAll(); }, [csrf]);
  useEffect(() => {
    if (!csrf) return;
    const timer = window.setInterval(() => void loadAll(), 15_000);
    return () => window.clearInterval(timer);
  }, [csrf]);

  async function logout() {
    try { await api("/api/v1/auth/logout", { method: "POST" }, csrf); } finally { setCsrf(""); setUsername(""); }
  }
  async function setPaused(paused: boolean) {
    try {
      const next = await api<typeof control>("/api/v1/control", { method: "PUT", body: JSON.stringify({ content_mutations_paused: paused, expected_revision: control.revision }) }, csrf);
      setControl(next);
    } catch (e) { setError(e instanceof Error ? e.message : "Gagal mengubah kontrol"); }
  }

  const activeCount = useMemo(() => blogs.filter((blog) => blog.active).length, [blogs]);
  if (!csrf) return <Login onLogin={(token, name) => { setCsrf(token); setUsername(name); }} />;

  const pageTitle: Record<Page, string> = { overview: "Ringkasan", blogs: "Blog profiles", providers: "Model providers", runs: "Run history", settings: "Kontrol sistem" };
  return <div className="min-h-screen bg-mist text-ink">
    <aside className="sidebar fixed inset-y-0 left-0 hidden w-64 flex-col border-r border-slate-200 bg-white px-5 py-6 lg:flex">
      <div className="flex items-center gap-3 px-2"><div className="grid h-10 w-10 place-items-center rounded-xl bg-teal-50 font-bold text-teal-600">T</div><div><p className="font-bold">Takumi</p><p className="text-xs text-slate-500">Research Agent</p></div></div>
      <nav className="mt-10 space-y-1">{(["overview", "blogs", "providers", "runs", "settings"] as Page[]).map((item) => <button key={item} onClick={() => setPage(item)} className={`nav-item ${page === item ? "nav-item-active" : ""}`}><span className="nav-dot" />{pageTitle[item]}</button>)}</nav>
      <div className="mt-auto rounded-2xl bg-slate-50 p-4"><p className="text-xs font-semibold uppercase tracking-wide text-slate-500">Konten keluar</p><p className="mt-2 text-sm font-semibold">{control.content_mutations_paused ? "Dijeda" : "Aktif"}</p><p className="mt-1 text-xs leading-5 text-slate-500">Profil baru tetap nonaktif sampai adapter dibaca dan divalidasi.</p></div>
    </aside>
    <main className="min-h-screen lg:ml-64">
      <header className="sticky top-0 z-10 flex items-center justify-between border-b border-slate-200 bg-white/90 px-5 py-4 backdrop-blur md:px-8">
        <div><p className="text-xs font-semibold uppercase tracking-[.18em] text-teal-600">Control plane</p><h1 className="mt-1 text-xl font-bold">{pageTitle[page]}</h1></div>
        <div className="flex items-center gap-3"><span className="hidden text-sm text-slate-500 sm:inline">{username}</span><button className="secondary-button" onClick={logout}>Keluar</button></div>
      </header>
      <div className="mx-auto max-w-7xl space-y-6 px-5 py-7 md:px-8">
        {error && <ErrorText>{error}</ErrorText>}
        {loading ? <div className="panel p-10 text-center text-slate-500">Memuat data…</div> : <>
          {page === "overview" && <Overview blogs={blogs} providers={providers} runs={runs} control={control} onOpen={setPage} />}
          {page === "blogs" && <section className="space-y-5"><div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="section-title">Koneksi blog</h2><p className="section-subtitle">Maksimal 10 profil. Struktur JSON dipetakan per blog dan diuji sebelum digunakan.</p></div><button className="primary-button" disabled={blogs.length >= 10} onClick={() => setShowBlogForm(true)}>Tambah blog <span className="ml-2 opacity-75">{blogs.length}/10</span></button></div>{blogs.length ? <div className="grid gap-4 xl:grid-cols-2">{blogs.map((blog) => <BlogCard key={blog.id} blog={blog} providers={providers} csrf={csrf} onRefresh={loadAll} />)}</div> : <EmptyState title="Belum ada blog" body="Tambahkan blog API dan OpenAPI contract atau mapping contoh response untuk memulai." />}</section>}
          {page === "providers" && <section className="space-y-5"><div className="flex flex-wrap items-center justify-between gap-3"><div><h2 className="section-title">Model providers</h2><p className="section-subtitle">OpenAI-compatible endpoints dan Anthropic Messages API. Credential tersimpan terenkripsi.</p></div><button className="primary-button" onClick={() => setShowProviderForm(true)}>Tambah provider</button></div>{providers.length ? <div className="grid gap-4 xl:grid-cols-2">{providers.map((provider) => <ProviderCard key={provider.id} provider={provider} csrf={csrf} onEdit={() => { setEditingProvider(provider); setShowProviderForm(true); }} onRefresh={loadAll} />)}</div> : <EmptyState title="Belum ada model" body="Simpan provider dulu. Probe akan mengirim satu request model dan bisa memakai token." />}</section>}
          {page === "runs" && <RunsPage runs={runs} blogs={blogs} />}
          {page === "settings" && <SettingsPage control={control} activeCount={activeCount} onPause={setPaused} />}
        </>}
      </div>
    </main>
    {showBlogForm && <BlogModal providers={providers} csrf={csrf} onClose={() => setShowBlogForm(false)} onSaved={() => { setShowBlogForm(false); void loadAll(); }} />}
    {showProviderForm && <ProviderModal provider={editingProvider} csrf={csrf} onClose={() => { setShowProviderForm(false); setEditingProvider(null); }} onSaved={() => { setShowProviderForm(false); setEditingProvider(null); void loadAll(); }} />}
  </div>;
}

function Overview({ blogs, providers, runs, control, onOpen }: { blogs: Blog[]; providers: Provider[]; runs: Run[]; control: { content_mutations_paused: boolean; recovery_mode: boolean }; onOpen: (page: Page) => void }) {
  const recent = runs.slice(0, 5);
  const cards = [
    { label: "Blog profiles", value: `${blogs.length}/10`, hint: `${blogs.filter((b) => b.active).length} aktif`, tone: "teal" },
    { label: "Provider model", value: providers.length, hint: `${providers.filter((p) => p.active).length} tersedia`, tone: "blue" },
    { label: "Run tercatat", value: runs.length, hint: runs[0] ? `Terbaru ${new Date(runs[0].created_at).toLocaleDateString("id-ID")}` : "Belum ada run", tone: "violet" },
    { label: "Mode mutasi", value: control.recovery_mode ? "Recovery" : control.content_mutations_paused ? "Pause" : "Aktif", hint: control.recovery_mode ? "Rekonsiliasi diperlukan" : "Global content control", tone: control.content_mutations_paused ? "amber" : "teal" },
  ];
  return <div className="space-y-6">
    <div className="rounded-3xl bg-gradient-to-br from-ink via-slate-800 to-slate-700 p-7 text-white shadow-panel md:p-9"><div className="max-w-2xl"><p className="text-xs font-semibold uppercase tracking-[.22em] text-teal-300">Daily research operations</p><h2 className="mt-3 text-3xl font-bold tracking-tight">Riset yang bisa ditelusuri, konten yang terkontrol.</h2><p className="mt-3 max-w-xl text-sm leading-6 text-slate-300">Konfigurasikan model dan blog, lihat hasil setiap tahap, dan tahan publikasi saat evidence atau integrasi belum lolos validasi.</p><button className="mt-6 rounded-xl bg-teal-500 px-4 py-2.5 text-sm font-semibold text-white hover:bg-teal-600" onClick={() => onOpen("blogs")}>Atur blog <span aria-hidden="true">→</span></button></div></div>
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">{cards.map((card) => <div className="panel p-5" key={card.label}><div className="flex items-center justify-between"><p className="text-sm text-slate-500">{card.label}</p><span className={`metric-mark metric-${card.tone}`} /></div><p className="mt-4 text-3xl font-bold">{card.value}</p><p className="mt-1 text-xs text-slate-500">{card.hint}</p></div>)}</div>
    <div className="grid gap-5 xl:grid-cols-[1.5fr_1fr]"><section className="panel"><div className="panel-heading"><div><h3 className="font-semibold">Run terakhir</h3><p className="mt-1 text-sm text-slate-500">Status pipeline dan target waktu setiap blog.</p></div><button className="text-sm font-semibold text-teal-600" onClick={() => onOpen("runs")}>Semua run →</button></div>{recent.length ? <RunTable runs={recent} blogs={blogs} /> : <div className="px-6 py-12 text-center"><p className="font-semibold">Belum ada aktivitas</p><p className="mt-1 text-sm text-slate-500">Jadwal belum berjalan; konfigurasi awal dilakukan di Blog profiles.</p></div>}</section>
      <section className="panel p-6"><h3 className="font-semibold">Sebelum jadwal diaktifkan</h3><ol className="mt-5 space-y-4">{[{n:"01",title:"Sambungkan model",done:providers.length > 0},{n:"02",title:"Tambahkan kontrak blog",done:blogs.length > 0},{n:"03",title:"Validasi read mapping",done:blogs.some((b) => b.capability_result.list_posts)},{n:"04",title:"Uji sandbox create",done:blogs.some((b) => b.capability_result.sandbox_create)}].map((step) => <li className="flex gap-3" key={step.n}><span className={`step-badge ${step.done ? "step-done" : ""}`}>{step.done ? "✓" : step.n}</span><div><p className="text-sm font-medium">{step.title}</p><p className="mt-0.5 text-xs text-slate-500">{step.done ? "Tercatat" : "Menunggu konfigurasi"}</p></div></li>)}</ol><div className="mt-6 rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs leading-5 text-amber-900">Validasi read saja belum membuktikan create, publish, idempotency, atau remote scheduling. Konten keluar tetap dijeda sampai alur deployment dan sandbox diuji.</div></section></div>
  </div>;
}

function BlogCard({ blog, providers, csrf, onRefresh }: { blog: Blog; providers: Provider[]; csrf: string; onRefresh: () => Promise<void> }) {
  const [ops, setOps] = useState<{ operation_id: string; method: string; path: string }[]>([]);
  const [selected, setSelected] = useState(blog.capabilities.list_operation?.operation_id ?? "");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [topicOpen, setTopicOpen] = useState(false);
  const [mappingOpen, setMappingOpen] = useState(false);
  const [mappingText, setMappingText] = useState(JSON.stringify(blog.adapter_mapping, null, 2));
  const [sampleText, setSampleText] = useState("{\n  \"data\": { \"items\": [{ \"id\": \"123\", \"title\": \"Example\", \"content\": \"Body\" }] }\n}");
  const [canonicalText, setCanonicalText] = useState("{\n  \"title\": \"Example title\",\n  \"content\": \"Example body\",\n  \"summary\": \"Example summary\",\n  \"locale\": \"en\",\n  \"status\": \"draft\"\n}");
  const [preview, setPreview] = useState("");
  const [editOpen, setEditOpen] = useState(false);
  const [runBusy, setRunBusy] = useState(false);
  async function loadContract() {
    setBusy(true); setError("");
    try { const result = await api<{ operations: typeof ops }>(`/api/v1/blogs/${blog.id}/adapter/contract`); setOps(result.operations); const first = result.operations.find((op) => op.method === "get"); if (first && !selected) setSelected(first.operation_id); }
    catch (e) { setError(e instanceof Error ? e.message : "Gagal membaca contract"); }
    finally { setBusy(false); }
  }
  async function testRead() {
    setBusy(true); setError("");
    try { await api(`/api/v1/blogs/${blog.id}/adapter/test-read`, { method: "POST", body: JSON.stringify({ list_operation_id: selected }) }, csrf); await onRefresh(); }
    catch (e) { setError(e instanceof Error ? e.message : "Read test gagal"); }
    finally { setBusy(false); }
  }
  async function toggleActive(active: boolean) {
    setBusy(true); setError("");
    try {
      await api(`/api/v1/blogs/${blog.id}`, { method: "PUT", body: JSON.stringify({
        name: blog.name, base_url: blog.base_url, api_spec_source: blog.api_spec_source,
        auth_header: blog.auth_header, auth_scheme: blog.auth_scheme, provider_id: blog.provider_id,
        adapter_mapping: blog.adapter_mapping, capabilities: blog.capabilities, topic_settings: blog.topic_settings,
        editorial_settings: blog.editorial_settings, schedule_enabled: blog.schedule_enabled,
        schedule_timezone: blog.schedule_timezone, publish_time: blog.publish_time, lead_minutes: blog.lead_minutes,
        active, expected_revision: blog.revision,
      }) }, csrf);
      await onRefresh();
    } catch (e) { setError(e instanceof Error ? e.message : "Gagal memperbarui profil"); }
    finally { setBusy(false); }
  }
  async function previewMapping() {
    setBusy(true); setError(""); setPreview("");
    try {
      const result = await api<Record<string, unknown>>(`/api/v1/blogs/${blog.id}/adapter/preview`, { method: "POST", body: JSON.stringify({ sample_response: JSON.parse(sampleText), canonical_post: JSON.parse(canonicalText) }) }, csrf);
      setPreview(JSON.stringify(result, null, 2));
    } catch (e) { setError(e instanceof Error ? e.message : "Preview gagal; periksa JSON mapping dan sample."); }
    finally { setBusy(false); }
  }
  async function saveMapping() {
    setBusy(true); setError("");
    try {
      const mapping = JSON.parse(mappingText);
      await api(`/api/v1/blogs/${blog.id}`, { method: "PUT", body: JSON.stringify({
        name: blog.name, base_url: blog.base_url, api_spec_source: blog.api_spec_source,
        auth_header: blog.auth_header, auth_scheme: blog.auth_scheme, provider_id: blog.provider_id,
        adapter_mapping: mapping, capabilities: blog.capabilities, topic_settings: blog.topic_settings,
        editorial_settings: blog.editorial_settings, schedule_enabled: blog.schedule_enabled,
        schedule_timezone: blog.schedule_timezone, publish_time: blog.publish_time, lead_minutes: blog.lead_minutes,
        active: false, expected_revision: blog.revision,
      }) }, csrf);
      setMappingOpen(false); setPreview(""); await onRefresh();
    } catch (e) { setError(e instanceof Error ? e.message : "Mapping JSON tidak valid"); }
    finally { setBusy(false); }
  }
  async function startDryRun() {
    setRunBusy(true); setError("");
    try {
      await api("/api/v1/blogs/" + blog.id + "/runs", { method: "POST" }, csrf);
      await onRefresh();
    } catch (e) { setError(e instanceof Error ? e.message : "Gagal mengantrikan dry-run"); }
    finally { setRunBusy(false); }
  }
  const providerName = providers.find((p) => p.id === blog.provider_id)?.name ?? "Belum dipilih";
  const providerReady = providers.some((p) => p.id === blog.provider_id && p.active && p.capability_result.status === "reachable");
  return <article className="panel overflow-hidden"><div className="flex items-start justify-between gap-4 border-b border-slate-100 p-5"><div className="min-w-0"><div className="flex items-center gap-2"><h3 className="truncate font-semibold">{blog.name}</h3><span className={`status-pill ${blog.active ? "status-green" : "status-gray"}`}>{blog.active ? "Aktif" : "Nonaktif"}</span></div><p className="mt-1 truncate text-xs text-slate-500">{blog.base_url}</p></div><span className="rounded-lg bg-slate-100 px-2.5 py-1 text-xs font-medium text-slate-600">rev {blog.revision}</span></div>
    <div className="flex flex-wrap gap-2 px-5"><button className="secondary-button" onClick={() => setEditOpen(true)}>Edit profil</button><button className="secondary-button" disabled={!blog.active || runBusy} onClick={() => void startDryRun()}>{runBusy ? "Mengantrikan…" : "Jalankan dry-run"}</button><p className="w-full text-xs text-slate-500">Dry-run memakai provider dan sumber eksternal, tetapi hasilnya hanya tersimpan lokal; tidak ada penulisan ke blog.</p></div>
    <div className="grid gap-3 p-5 sm:grid-cols-3"><MetricSmall label="Provider" value={providerName} /><MetricSmall label="Jadwal" value={blog.schedule_enabled ? `${blog.publish_time} ${blog.schedule_timezone}` : "Nonaktif"} /><MetricSmall label="History read" value={blog.capability_result.list_posts ? `${blog.capability_result.records_read ?? 0} artikel` : "Belum dites"} /></div>
    <div className="space-y-4 border-t border-slate-100 p-5"><div className="flex flex-wrap gap-2"><button className="secondary-button" onClick={() => void loadContract()} disabled={busy}>{busy ? "Memuat…" : "Baca OpenAPI"}</button>{ops.length > 0 && <><select aria-label="List GET operation" value={selected} onChange={(e) => setSelected(e.target.value)} className="field-select min-w-48 flex-1"><option value="">Pilih GET list operation</option>{ops.filter((op) => op.method === "get").map((op) => <option key={op.operation_id} value={op.operation_id}>{op.operation_id} · {op.path}</option>)}</select><button className="secondary-button" onClick={() => void testRead()} disabled={busy || !selected}>Test read-only GET</button></>}</div>
      <div className="flex flex-wrap items-center justify-between gap-2"><div className="flex gap-4"><button className="text-sm font-medium text-slate-600 hover:text-ink" onClick={() => setTopicOpen(!topicOpen)}>{topicOpen ? "Sembunyikan cakupan" : "Lihat cakupan topik"}</button><button className="text-sm font-medium text-slate-600 hover:text-ink" onClick={() => { setMappingText(JSON.stringify(blog.adapter_mapping, null, 2)); setMappingOpen(!mappingOpen); }}>JSON mapping</button></div><button className={blog.active ? "danger-button" : "secondary-button"} disabled={busy || (!blog.active && (!blog.capability_result.history_complete || !providerReady))} onClick={() => void toggleActive(!blog.active)}>{blog.active ? "Nonaktifkan profil" : "Aktifkan untuk riset"}</button></div>
      {topicOpen && <div className="flex flex-wrap gap-2">{fields.map((field) => <span className={`topic-chip ${blog.topic_settings[field] ? "topic-on" : ""}`} key={field}>{field}</span>)}</div>}
      {mappingOpen && <div className="space-y-3 rounded-xl border border-slate-200 bg-slate-50 p-4"><p className="text-sm font-semibold">Response dan payload mapping</p><label className="field-label">JSONPath mapping<textarea aria-label="JSONPath mapping" value={mappingText} onChange={(e) => setMappingText(e.target.value)} rows={8} className="field-input font-mono text-xs" placeholder={'{"list_path":"$.data.items","response_fields":{"id":"$.id","title":"$.title","content":"$.content"},"create_fields":{"title":"$.article.title","content":"$.article.body","status":"$.article.status"}}'} /></label><label className="field-label">Sample API response<textarea aria-label="Sample API response" value={sampleText} onChange={(e) => setSampleText(e.target.value)} rows={5} className="field-input font-mono text-xs" /></label><label className="field-label">Canonical create sample<textarea aria-label="Canonical create sample" value={canonicalText} onChange={(e) => setCanonicalText(e.target.value)} rows={4} className="field-input font-mono text-xs" /></label><div className="flex gap-2"><button className="secondary-button" onClick={() => void previewMapping()} disabled={busy}>Preview only</button><button className="primary-button" onClick={() => void saveMapping()} disabled={busy}>Simpan mapping</button></div>{preview && <pre className="max-h-64 overflow-auto rounded-xl bg-ink p-3 text-xs text-teal-100">{preview}</pre>}</div>}
      <p className="text-xs leading-5 text-slate-500">OpenAPI operation list: {blog.api_spec_source || "belum dikonfigurasi"}. Profile activation hanya membuka riset/history; write capability belum terbukti.</p>{error && <ErrorText>{error}</ErrorText>}
    </div>
    {editOpen && <BlogModal blog={blog} providers={providers} csrf={csrf} onClose={() => setEditOpen(false)} onSaved={() => { setEditOpen(false); void onRefresh(); }} />}

  </article>;
}

function ProviderCard({ provider, csrf, onEdit, onRefresh }: { provider: Provider; csrf: string; onEdit: () => void; onRefresh: () => Promise<void> }) {
  const [busy, setBusy] = useState(false); const [error, setError] = useState("");
  async function probe() {
    setBusy(true); setError("");
    try { await api(`/api/v1/providers/${provider.id}/probe`, { method: "POST" }, csrf); await onRefresh(); }
    catch (e) { setError(e instanceof Error ? e.message : "Probe gagal; request mungkin menggunakan token."); await onRefresh(); }
    finally { setBusy(false); }
  }
  return <article className="panel p-5"><div className="flex items-start justify-between gap-4"><div><h3 className="font-semibold">{provider.name}</h3><p className="mt-1 text-xs text-slate-500">{provider.provider_type === "anthropic" ? "Anthropic Messages API" : "OpenAI-compatible"}</p></div><span className={`status-pill ${provider.secret_configured ? "status-green" : "status-amber"}`}>{provider.secret_configured ? "Credential tersimpan" : "Tanpa credential"}</span></div><div className="mt-5 grid gap-3 sm:grid-cols-2"><MetricSmall label="Model" value={provider.model} /><MetricSmall label="Endpoint" value={provider.base_url} /><MetricSmall label="Timeout" value={`${provider.timeout_seconds}s`} /><MetricSmall label="Probe" value={String(provider.capability_result.status ?? "Belum dites")} /></div><div className="mt-4 rounded-xl bg-slate-50 p-3 text-xs leading-5 text-slate-600">Probe mengirim satu request model berbiaya. Hasilnya hanya membuktikan balasan text, bukan tool calling, JSON schema, atau kapasitas konteks.</div><div className="mt-4 flex gap-2"><button className="secondary-button" onClick={onEdit}>Edit config</button><button className="secondary-button" disabled={busy} onClick={() => void probe()}>{busy ? "Probe berjalan…" : "Probe model"}</button></div>{error && <ErrorText>{error}</ErrorText>}</article>;
}

function ProviderModal({ provider, csrf, onClose, onSaved }: { provider: Provider | null; csrf: string; onClose: () => void; onSaved: () => void }) {
  const [error, setError] = useState(""); const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault(); setBusy(true); setError(""); const data = new FormData(event.currentTarget);
    try {
      const payload = { name: data.get("name"), provider_type: data.get("provider_type"), base_url: data.get("base_url"), model: data.get("model"), api_key: data.get("api_key") || null, timeout_seconds: Number(data.get("timeout_seconds")), max_output_tokens: Number(data.get("max_output_tokens")), ...(provider ? { expected_revision: provider.revision } : {}) };
      await api(provider ? `/api/v1/providers/${provider.id}` : "/api/v1/providers", { method: provider ? "PUT" : "POST", body: JSON.stringify(payload) }, csrf);
      onSaved();
    } catch (e) { setError(e instanceof Error ? e.message : "Gagal menyimpan provider"); }
    finally { setBusy(false); }
  }
  return <Modal title={provider ? "Edit model provider" : "Tambah model provider"} onClose={onClose}><form onSubmit={submit} className="space-y-4"><label className="field-label">Nama<input name="name" required minLength={2} className="field-input" placeholder="Research model" defaultValue={provider?.name} /></label><label className="field-label">Provider<select name="provider_type" className="field-select" defaultValue={provider?.provider_type ?? "openai_compatible"}><option value="openai_compatible">OpenAI-compatible</option><option value="anthropic">Anthropic</option></select></label><label className="field-label">Base URL<input name="base_url" type="url" required className="field-input" defaultValue={provider?.base_url ?? "https://api.openai.com/v1"} /></label><label className="field-label">Model ID<input name="model" required className="field-input" placeholder="Isi sesuai provider endpoint" defaultValue={provider?.model} /></label><label className="field-label">API key<input name="api_key" type="password" autoComplete="new-password" className="field-input" placeholder={provider?.secret_configured ? "Kosongkan untuk mempertahankan key saat ini" : "Tersimpan terenkripsi; tidak ditampilkan lagi"} /></label><div className="grid grid-cols-2 gap-3"><label className="field-label">Timeout (s)<input name="timeout_seconds" type="number" min="5" max="600" defaultValue={provider?.timeout_seconds ?? 90} className="field-input" /></label><label className="field-label">Output tokens<input name="max_output_tokens" type="number" min="128" max="64000" defaultValue={provider?.max_output_tokens ?? 4000} className="field-input" /></label></div>{error && <ErrorText>{error}</ErrorText>}<ModalActions onClose={onClose} busy={busy} submitLabel={provider ? "Simpan perubahan" : "Simpan provider"} /></form></Modal>;
}

function BlogModal({ blog, providers, csrf, onClose, onSaved }: { blog?: Blog; providers: Provider[]; csrf: string; onClose: () => void; onSaved: () => void }) {
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const data = new FormData(event.currentTarget);
    try {
      const payload = {
        name: data.get("name"),
        base_url: data.get("base_url"),
        api_spec_source: data.get("api_spec_source"),
        auth_header: data.get("auth_header") || "Authorization",
        auth_scheme: data.get("auth_scheme") || "Bearer",
        api_token: data.get("api_token") || null,
        provider_id: data.get("provider_id") || null,
        schedule_enabled: data.get("schedule_enabled") === "on",
        schedule_timezone: data.get("schedule_timezone"),
        publish_time: data.get("publish_time"),
        lead_minutes: Number(data.get("lead_minutes")),
        topic_settings: Object.fromEntries(fields.map((field) => [field, data.get("topic_" + field) === "on"])),
        ...(blog ? { adapter_mapping: blog.adapter_mapping, capabilities: blog.capabilities, editorial_settings: blog.editorial_settings, active: false, expected_revision: blog.revision } : {}),
      };
      await api(blog ? "/api/v1/blogs/" + blog.id : "/api/v1/blogs", {
        method: blog ? "PUT" : "POST",
        body: JSON.stringify(payload),
      }, csrf);
      onSaved();
    } catch (e) {
      setError(e instanceof Error ? e.message : "Gagal menyimpan blog");
    } finally {
      setBusy(false);
    }
  }
  return <Modal title={blog ? "Edit blog profile" : "Tambah blog profile"} onClose={onClose}>
    <form onSubmit={submit} className="space-y-4">
      <label className="field-label">Nama profile<input name="name" required className="field-input" placeholder="Blog teknologi" defaultValue={blog?.name} /></label>
      <label className="field-label">Base URL<input name="base_url" type="url" required className="field-input" placeholder="https://blog.example.com/api" defaultValue={blog?.base_url} /></label>
      <label className="field-label">OpenAPI JSON/YAML source<input name="api_spec_source" className="field-input" placeholder="URL HTTPS allowlisted atau file di server" defaultValue={blog?.api_spec_source} /></label>
      <div className="grid grid-cols-2 gap-3">
        <label className="field-label">Auth header<input name="auth_header" className="field-input" defaultValue={blog?.auth_header ?? "Authorization"} /></label>
        <label className="field-label">Auth scheme<input name="auth_scheme" className="field-input" defaultValue={blog?.auth_scheme ?? "Bearer"} /></label>
      </div>
      <label className="field-label">API token<input name="api_token" type="password" autoComplete="new-password" className="field-input" placeholder={blog?.secret_configured ? "Kosongkan untuk mempertahankan token" : "Credential blog"} /></label>
      <label className="field-label">Model provider<select name="provider_id" className="field-select" defaultValue={blog?.provider_id ?? ""}><option value="">Pilih nanti</option>{providers.map((p) => <option key={p.id} value={p.id}>{p.name} · {p.model}</option>)}</select></label>
      <div className="grid grid-cols-3 gap-3">
        <label className="field-label">Timezone<input name="schedule_timezone" defaultValue={blog?.schedule_timezone ?? "Asia/Jakarta"} className="field-input" /></label>
        <label className="field-label">Target time<input name="publish_time" type="time" defaultValue={blog?.publish_time ?? "06:00"} className="field-input" /></label>
        <label className="field-label">Lead (min)<input name="lead_minutes" type="number" defaultValue={blog?.lead_minutes ?? 120} min="0" max="1440" className="field-input" /></label>
      </div>
      <label className="flex items-center gap-2 text-sm text-slate-700"><input name="schedule_enabled" type="checkbox" defaultChecked={blog?.schedule_enabled ?? false} className="accent-teal-600" />Jadwalkan otomatis setiap hari</label>
      <div>
        <p className="field-label">Cakupan topik</p>
        <div className="mt-2 flex flex-wrap gap-4">{fields.map((field) => <label key={field} className="flex items-center gap-2 text-sm capitalize text-slate-700"><input name={"topic_" + field} type="checkbox" defaultChecked={blog?.topic_settings[field] ?? true} className="accent-teal-600" />{field}</label>)}</div>
      </div>
      <div className="rounded-xl bg-slate-50 p-3 text-xs leading-5 text-slate-600">Profil dimulai nonaktif. Perubahan endpoint, contract, token, atau mapping meminta validasi GET ulang. Publish otomatis baru dibuka setelah write capability diuji.</div>
      {error && <ErrorText>{error}</ErrorText>}
      <ModalActions onClose={onClose} busy={busy} submitLabel={blog ? "Simpan perubahan" : "Simpan profil"} />
    </form>
  </Modal>;
}
function RunsPage({ runs, blogs }: { runs: Run[]; blogs: Blog[] }) {
  const [detail, setDetail] = useState<Record<string, any> | null>(null);
  const [locale, setLocale] = useState<"id" | "en">("id");
  const [error, setError] = useState("");
  async function openRun(id: string) {
    setError("");
    try { setDetail(await api<Record<string, any>>("/api/v1/runs/" + id)); setLocale("id"); }
    catch (e) { setError(e instanceof Error ? e.message : "Gagal memuat detail run"); }
  }
  const article = detail?.pair?.articles?.find((item: any) => item.locale === locale);
  return <section className="space-y-5"><div><h2 className="section-title">Run history</h2><p className="section-subtitle">Outcome pipeline, keputusan editorial, status pasangan bahasa, dan deadline.</p></div>{error && <ErrorText>{error}</ErrorText>}{runs.length ? <div className="panel overflow-hidden"><RunTable runs={runs} blogs={blogs} onOpen={openRun} /></div> : <EmptyState title="Belum ada run" body="Belum ada aktivitas riset atau scheduled slot yang tercatat." />}{detail && <Modal title={detail.pair?.topic ?? "Detail run"} onClose={() => setDetail(null)}><div className="space-y-4"><div className="grid grid-cols-2 gap-4 rounded-xl bg-slate-50 p-4"><MetricSmall label="Status" value={detail.status} /><MetricSmall label="Keputusan" value={detail.editorial_decision ?? "pending"} /><MetricSmall label="Outcome" value={detail.pipeline_outcome ?? "—"} /><MetricSmall label="Pair" value={detail.pair_completeness ?? "none"} /></div>{detail.pair ? <><div className="flex items-center justify-between gap-3"><div><p className="font-semibold">{detail.pair.topic}</p><p className="mt-1 text-xs text-slate-500">{detail.pair.research_question}</p></div><div className="flex gap-2"><button className={locale === "id" ? "primary-button" : "secondary-button"} onClick={() => setLocale("id")}>ID</button><button className={locale === "en" ? "primary-button" : "secondary-button"} onClick={() => setLocale("en")}>EN</button></div></div>{article && <div className="rounded-2xl border border-slate-200 p-5"><h3 className="text-lg font-bold">{article.title}</h3><p className="mt-2 text-sm text-slate-500">{article.summary}</p><pre className="mt-5 max-h-[55vh] overflow-auto whitespace-pre-wrap font-sans text-sm leading-7 text-slate-700">{article.body}</pre></div>}<details className="rounded-xl bg-slate-50 p-4"><summary className="cursor-pointer text-sm font-semibold">Hasil review dan kebijakan</summary><pre className="mt-3 overflow-auto whitespace-pre-wrap text-xs leading-5">{JSON.stringify(detail.pair.review, null, 2)}</pre></details></> : <p className="text-sm text-slate-500">Run ini belum menghasilkan pasangan artikel.</p>}</div></Modal>}</section>;
}

function RunTable({ runs, blogs, onOpen }: { runs: Run[]; blogs: Blog[]; onOpen?: (id: string) => void }) {
  return <div className="overflow-x-auto"><table className="w-full min-w-[760px] text-left text-sm"><thead className="bg-slate-50 text-xs uppercase tracking-wide text-slate-500"><tr><th className="px-5 py-3">Blog / tanggal</th><th className="px-5 py-3">Stage</th><th className="px-5 py-3">Pipeline</th><th className="px-5 py-3">Editorial</th><th className="px-5 py-3">Deadline</th><th className="px-5 py-3">Target</th>{onOpen && <th className="px-5 py-3">Detail</th>}</tr></thead><tbody className="divide-y divide-slate-100">{runs.map((run) => <tr key={run.id}><td className="px-5 py-4"><p className="font-medium">{blogs.find((blog) => blog.id === run.blog_id)?.name ?? "Blog dihapus"}</p><p className="mt-1 text-xs text-slate-500">{run.editorial_date}</p></td><td className="px-5 py-4"><span className="status-pill status-blue">{run.stage}</span></td><td className="px-5 py-4">{run.status}</td><td className="px-5 py-4">{run.editorial_decision}</td><td className="px-5 py-4">{run.deadline_result}</td><td className="px-5 py-4 text-xs text-slate-500">{new Date(run.target_at).toLocaleString("id-ID")}</td>{onOpen && <td className="px-5 py-4"><button className="text-sm font-semibold text-teal-700" onClick={() => onOpen(run.id)}>Buka</button></td>}</tr>)}</tbody></table></div>;
}

function SettingsPage({ control, activeCount, onPause }: { control: { content_mutations_paused: boolean; recovery_mode: boolean }; activeCount: number; onPause: (paused: boolean) => Promise<void> }) {
  return <section className="max-w-3xl space-y-5"><div><h2 className="section-title">Kontrol sistem</h2><p className="section-subtitle">Jeda mutation konten secara global. Reconciliation dan pembatalan schedule terkelola tetap mengikuti alur khusus.</p></div><div className="panel p-6"><div className="flex flex-wrap items-start justify-between gap-5"><div><h3 className="font-semibold">Publish kill switch</h3><p className="mt-2 max-w-xl text-sm leading-6 text-slate-500">Saat pause, intent baru untuk membuat, mengubah, atau menerbitkan artikel ditahan. Request yang sudah terkirim tetap perlu direkonsiliasi.</p></div><button className={control.content_mutations_paused ? "primary-button" : "danger-button"} disabled={control.recovery_mode} onClick={() => void onPause(!control.content_mutations_paused)}>{control.content_mutations_paused ? "Buka konten keluar" : "Pause konten keluar"}</button></div><div className="mt-6 grid gap-3 sm:grid-cols-3"><MetricSmall label="Recovery mode" value={control.recovery_mode ? "Aktif" : "Tidak aktif"} /><MetricSmall label="Profil aktif" value={String(activeCount)} /><MetricSmall label="Default schedule" value="06:00 Asia/Jakarta" /></div>{control.recovery_mode && <ErrorText>Recovery mode aktif. Mutation tetap dijeda sampai rekonsiliasi restore selesai melalui proses operasional.</ErrorText>}</div><div className="panel p-6"><h3 className="font-semibold">Batas yang diterapkan</h3><ul className="mt-4 space-y-2 text-sm text-slate-600"><li>• Maksimal 10 blog profiles.</li><li>• Profil baru nonaktif sampai history mapping tervalidasi.</li><li>• Belum ada automatic publishing sampai create capability lolos sandbox.</li><li>• Provider probe dan pengiriman sandbox memakai request/token eksternal.</li></ul></div></section>;
}

function MetricSmall({ label, value }: { label: string; value: string }) { return <div className="min-w-0"><p className="text-[11px] font-semibold uppercase tracking-wide text-slate-400">{label}</p><p className="mt-1 truncate text-sm font-medium text-slate-700">{value}</p></div>; }
function EmptyState({ title, body }: { title: string; body: string }) { return <div className="panel px-6 py-14 text-center"><div className="mx-auto grid h-12 w-12 place-items-center rounded-2xl bg-teal-50 font-bold text-teal-600">T</div><h3 className="mt-4 font-semibold">{title}</h3><p className="mx-auto mt-1 max-w-md text-sm leading-6 text-slate-500">{body}</p></div>; }
function Modal({ title, children, onClose }: { title: string; children: React.ReactNode; onClose: () => void }) { return <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/50 p-4"><div role="dialog" aria-modal="true" aria-label={title} className="max-h-[90vh] w-full max-w-2xl overflow-y-auto rounded-3xl bg-white p-6 shadow-panel md:p-8"><div className="mb-6 flex items-center justify-between"><h2 className="text-lg font-bold">{title}</h2><button aria-label="Tutup" onClick={onClose} className="rounded-lg px-2 py-1 text-slate-400 hover:bg-slate-100">✕</button></div>{children}</div></div>; }
function ModalActions({ onClose, busy, submitLabel }: { onClose: () => void; busy: boolean; submitLabel: string }) { return <div className="flex justify-end gap-2 border-t border-slate-100 pt-4"><button type="button" className="secondary-button" onClick={onClose}>Batal</button><button className="primary-button" disabled={busy}>{busy ? "Menyimpan…" : submitLabel}</button></div>; }

export default App;
