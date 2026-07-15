// Simple Computers 101 license portal — customer self-service + admin.
// Manages licenses across every app sold (product id per license); X32
// SonicSniper is the first.
//
// Plain ES modules, Firebase Web SDK from the gstatic CDN (a cloud page is
// inherently online, so a CDN dependency is fine here). All license MUTATIONS
// go through Cloud Functions; the portal only READS Firestore directly, and
// the security rules limit a customer to their own rows.
import { initializeApp } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-app.js";
import {
  getAuth, onAuthStateChanged, signInWithEmailAndPassword,
  createUserWithEmailAndPassword, sendEmailVerification, sendPasswordResetEmail, signOut,
} from "https://www.gstatic.com/firebasejs/10.12.0/firebase-auth.js";
import {
  getFirestore, collection, query, where, getDocs,
} from "https://www.gstatic.com/firebasejs/10.12.0/firebase-firestore.js";
import { getFunctions, httpsCallable } from "https://www.gstatic.com/firebasejs/10.12.0/firebase-functions.js";
import { firebaseConfig, functionsRegion, functionsBaseUrl, storeProducts } from "./firebase-config.js";

const app = initializeApp(firebaseConfig);
const auth = getAuth(app);
const db = getFirestore(app);
const fns = getFunctions(app, functionsRegion);

const $ = (id) => document.getElementById(id);
const show = (id, on) => $(id).classList.toggle("hidden", !on);
const setStatus = (id, text, kind = "") => { const n = $(id); n.textContent = text; n.className = "status " + kind; };
const fmtDate = (v) => {
  if (!v) return "Never";
  if (typeof v === "string") return v;
  if (v.toDate) return v.toDate().toISOString().slice(0, 10);
  return String(v);
};

async function call(name, data) {
  const res = await httpsCallable(fns, name)(data);
  if (res.data && res.data.error) throw new Error(res.data.error);
  return res.data;
}

// -- storefront ---------------------------------------------------------------

function renderStore() {
  // Show the "thanks" banner if we came back from a successful Stripe checkout.
  if (new URLSearchParams(location.search).get("purchased") === "1") {
    show("purchased-card", true);
  }
  const host = $("store-products");
  host.innerHTML = "";
  (storeProducts || []).forEach((product) => {
    const wrap = document.createElement("div");
    wrap.style.marginBottom = "0.8rem";
    const title = document.createElement("div");
    title.innerHTML = `<b>${product.name}</b> — <span class="muted">${product.blurb || ""}</span>`;
    wrap.appendChild(title);
    const row = document.createElement("div");
    row.className = "row";
    row.style.marginTop = "0.4rem";
    product.plans.forEach((p) => {
      const btn = document.createElement("button");
      btn.style.flex = "0";
      btn.textContent = `${p.label} — ${p.price}`;
      btn.onclick = () => buy(product.productId, p.plan, btn);
      const cell = document.createElement("div");
      cell.style.flex = "0";
      cell.appendChild(btn);
      row.appendChild(cell);
    });
    wrap.appendChild(row);
    host.appendChild(wrap);
  });
}

async function buy(productId, plan, btn) {
  btn.disabled = true;
  setStatus("store-status", "Redirecting to secure checkout…");
  try {
    const resp = await fetch(`${functionsBaseUrl}/create_checkout_session`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ productId, plan }),
    });
    const data = await resp.json();
    if (!resp.ok || data.error) throw new Error(data.error || `HTTP ${resp.status}`);
    location.href = data.url;
  } catch (e) {
    setStatus("store-status", `Could not start checkout: ${e.message}`, "error");
    btn.disabled = false;
  }
}

renderStore();

// -- auth state ---------------------------------------------------------------

onAuthStateChanged(auth, async (user) => {
  if (!user) {
    $("who").textContent = "";
    show("signout", false); show("auth-card", true);
    show("verify-card", false); show("licenses-card", false); show("admin-card", false);
    return;
  }
  $("who").textContent = user.email;
  show("signout", true); show("auth-card", false);

  if (!user.emailVerified) {
    $("verify-email").textContent = user.email;
    show("verify-card", true); show("licenses-card", false); show("admin-card", false);
    return;
  }
  show("verify-card", false); show("licenses-card", true);

  await loadLicenses(user);
  const token = await user.getIdTokenResult();
  if (token.claims.admin === true) { show("admin-card", true); await loadAllLicenses(); }
});

// -- auth actions -------------------------------------------------------------

$("signin-btn").addEventListener("click", async () => {
  try {
    await signInWithEmailAndPassword(auth, $("email").value.trim(), $("password").value);
  } catch (e) { setStatus("auth-status", e.message, "error"); }
});

$("signup-btn").addEventListener("click", async () => {
  try {
    const cred = await createUserWithEmailAndPassword(auth, $("email").value.trim(), $("password").value);
    await sendEmailVerification(cred.user);
    setStatus("auth-status", "Account created — check your email to verify.", "success");
  } catch (e) { setStatus("auth-status", e.message, "error"); }
});

$("reset-btn").addEventListener("click", async () => {
  try {
    await sendPasswordResetEmail(auth, $("email").value.trim());
    setStatus("auth-status", "Password reset email sent.", "success");
  } catch (e) { setStatus("auth-status", e.message, "error"); }
});

$("signout").addEventListener("click", () => signOut(auth));
$("resend-btn").addEventListener("click", async () => {
  try { await sendEmailVerification(auth.currentUser); setStatus("verify-status", "Sent.", "success"); }
  catch (e) { setStatus("verify-status", e.message, "error"); }
});
$("reload-btn").addEventListener("click", () => location.reload());

// -- customer: my licenses ----------------------------------------------------

async function loadLicenses(user) {
  setStatus("licenses-status", "Loading…");
  const body = $("licenses-body");
  body.innerHTML = "";
  try {
    const q = query(collection(db, "licenses"), where("ownerEmail", "==", user.email.toLowerCase()));
    const snap = await getDocs(q);
    show("licenses-empty", snap.empty);
    show("licenses-table", !snap.empty);
    snap.forEach((doc) => body.appendChild(customerRow(doc.data())));
    setStatus("licenses-status", "");
  } catch (e) {
    setStatus("licenses-status", "Could not load licenses: " + e.message, "error");
  }
}

function customerRow(lic) {
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td>${lic.productId || "—"}</td>
    <td><code>${lic.key}</code></td>
    <td>${lic.type === "monthly" ? "Monthly" : "Lifetime"}</td>
    <td><span class="pill ${lic.status === "active" ? "active" : "disabled"}">${lic.status}</span></td>
    <td>${fmtDate(lic.expires)}</td>
    <td>${lic.machineCode ? "<span class='muted'>bound</span>" : "<span class='muted'>—</span>"}</td>
    <td class="actions"></td>`;
  const cell = tr.querySelector(".actions");
  const copy = document.createElement("button");
  copy.className = "secondary"; copy.textContent = "Copy key";
  copy.onclick = () => navigator.clipboard.writeText(lic.key);
  cell.appendChild(copy);
  if (lic.machineCode) {
    const move = document.createElement("button");
    move.textContent = "Move to a new PC";
    move.onclick = async () => {
      if (!confirm("Release this license from its current PC so you can activate it on another? " +
        "The app on the old PC will stop working after its next check.")) return;
      move.disabled = true;
      try { await call("deactivate", { key: lic.key }); await loadLicenses(auth.currentUser); }
      catch (e) { setStatus("licenses-status", "Could not release: " + e.message, "error"); move.disabled = false; }
    };
    cell.appendChild(move);
  }
  return tr;
}

// -- admin --------------------------------------------------------------------

$("a-create-btn").addEventListener("click", async () => {
  const email = $("a-email").value.trim().toLowerCase();
  const type = $("a-type").value;
  const expires = $("a-expires").value.trim() || null;
  if (!email) { setStatus("admin-status", "Customer email required.", "error"); return; }
  try {
    const res = await call("admin_create", {
      ownerEmail: email, ownerName: $("a-name").value.trim(), type, tier: "pro", expires,
      productId: $("a-product").value,
    });
    setStatus("admin-status", `Created ${res.key} for ${email}.`, "success");
    await loadAllLicenses();
  } catch (e) { setStatus("admin-status", "Create failed: " + e.message, "error"); }
});

$("a-refresh-btn").addEventListener("click", loadAllLicenses);
$("a-search").addEventListener("input", renderAdminRows);

let ALL = [];
async function loadAllLicenses() {
  try {
    const snap = await getDocs(collection(db, "licenses"));
    ALL = snap.docs.map((d) => d.data());
    renderAdminRows();
  } catch (e) { setStatus("admin-status", "Could not load: " + e.message, "error"); }
}

function renderAdminRows() {
  const filter = $("a-search").value.trim().toLowerCase();
  const body = $("admin-body");
  body.innerHTML = "";
  ALL.filter((l) => !filter
        || (l.ownerEmail || "").includes(filter)
        || (l.key || "").toLowerCase().includes(filter)
        || (l.productId || "").toLowerCase().includes(filter))
     .forEach((l) => body.appendChild(adminRow(l)));
}

function adminRow(lic) {
  const tr = document.createElement("tr");
  tr.innerHTML = `
    <td>${lic.productId || "—"}</td>
    <td><code>${lic.key}</code></td>
    <td>${lic.ownerEmail || ""}</td>
    <td>${lic.type}</td>
    <td><span class="pill ${lic.status === "active" ? "active" : "disabled"}">${lic.status}</span></td>
    <td>${fmtDate(lic.expires)}</td>
    <td>${lic.machineCode || "—"}</td>
    <td class="actions"></td>`;
  const cell = tr.querySelector(".actions");

  const toggle = document.createElement("button");
  toggle.className = lic.status === "active" ? "danger" : "";
  toggle.textContent = lic.status === "active" ? "Disable" : "Enable";
  toggle.onclick = () => adminUpdate({ key: lic.key, status: lic.status === "active" ? "disabled" : "active" });
  cell.appendChild(toggle);

  if (lic.machineCode) {
    const wipe = document.createElement("button");
    wipe.className = "secondary"; wipe.textContent = "Wipe machine";
    wipe.onclick = () => adminUpdate({ key: lic.key, machineCode: null });
    cell.appendChild(wipe);
  }

  const extend = document.createElement("button");
  extend.className = "secondary"; extend.textContent = "Set expiry";
  extend.onclick = () => {
    const v = prompt("New expiry (YYYY-MM-DD), or blank for never:", lic.expires || "");
    if (v === null) return;
    adminUpdate({ key: lic.key, expires: v.trim() || null });
  };
  cell.appendChild(extend);
  return tr;
}

async function adminUpdate(patch) {
  try { await call("admin_update", patch); await loadAllLicenses(); }
  catch (e) { setStatus("admin-status", "Update failed: " + e.message, "error"); }
}
