/*
 * admin.js
 * --------
 * Powers Tomy/admin.html. Talks to the Flask API in Tomy/admin-backend/.
 * Relies on API_BASE_URL and formatBirr() from products-api.js (loaded first).
 */

const TOKEN_KEY = "tomyAdminToken";
let allProducts = [];
let editingId = null;

// ---------- Elements ----------
const loginScreen = document.getElementById("loginScreen");
const adminScreen = document.getElementById("adminScreen");
const loginForm = document.getElementById("loginForm");
const passwordInput = document.getElementById("passwordInput");
const loginError = document.getElementById("loginError");
const logoutBtn = document.getElementById("logoutBtn");

const adminStatus = document.getElementById("adminStatus");
const tableBody = document.getElementById("productsTableBody");
const categoryFilter = document.getElementById("categoryFilter");
const tableSearch = document.getElementById("tableSearch");
const newProductBtn = document.getElementById("newProductBtn");

const editorOverlay = document.getElementById("editorOverlay");
const productForm = document.getElementById("productForm");
const editorTitle = document.getElementById("editorTitle");
const closeEditorBtn = document.getElementById("closeEditorBtn");
const cancelEditorBtn = document.getElementById("cancelEditorBtn");
const deleteProductBtn = document.getElementById("deleteProductBtn");
const formError = document.getElementById("formError");
const imagesList = document.getElementById("imagesList");
const addImageBtn = document.getElementById("addImageBtn");
const sizesList = document.getElementById("sizesList");
const addSizeBtn = document.getElementById("addSizeBtn");

const fieldPrice = document.getElementById("fieldPrice");
const fieldOldPrice = document.getElementById("fieldOldPrice");
const fieldDiscountPercent = document.getElementById("fieldDiscountPercent");
const discountPercentRow = document.getElementById("discountPercentRow");

// ---------- Auth helpers ----------
function getToken() {
  return localStorage.getItem(TOKEN_KEY);
}
function setToken(token) {
  localStorage.setItem(TOKEN_KEY, token);
}
function clearToken() {
  localStorage.removeItem(TOKEN_KEY);
}

async function apiFetch(path, options = {}) {
  const headers = options.headers || {};
  if (options.body) headers["Content-Type"] = "application/json";
  const token = getToken();
  if (token) headers["Authorization"] = `Bearer ${token}`;

  const res = await fetch(`${API_BASE_URL}${path}`, { ...options, headers });
  const data = await res.json().catch(() => ({}));

  if (res.status === 401) {
    clearToken();
    showLogin("Your session expired — please log in again.");
    throw new Error(data.error || "Unauthorized");
  }
  if (!res.ok) {
    throw new Error(data.error || `Request failed (${res.status})`);
  }
  return data;
}

function showLogin(message) {
  loginScreen.classList.remove("hidden");
  adminScreen.classList.add("hidden");
  loginError.textContent = message || "";
}
function showDashboard() {
  loginScreen.classList.add("hidden");
  adminScreen.classList.remove("hidden");
  loadProducts();
}

// ---------- Login ----------
loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  loginError.textContent = "";
  try {
    const res = await fetch(`${API_BASE_URL}/login`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: passwordInput.value }),
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Login failed");
    setToken(data.token);
    passwordInput.value = "";
    showDashboard();
  } catch (err) {
    loginError.textContent = err.message;
  }
});

logoutBtn.addEventListener("click", () => {
  clearToken();
  showLogin();
});

// ---------- Load & render table ----------
async function loadProducts() {
  adminStatus.textContent = "";
  tableBody.innerHTML = `<tr><td colspan="6" class="admin-loading">Loading products…</td></tr>`;
  try {
    allProducts = await apiFetch("/products");
    renderTable();
  } catch (err) {
    tableBody.innerHTML = `<tr><td colspan="6" class="admin-loading">${err.message}</td></tr>`;
  }
}

function renderTable() {
  const category = categoryFilter.value;
  const search = tableSearch.value.trim().toLowerCase();

  const rows = allProducts.filter((p) => {
    if (category && p.category !== category) return false;
    if (search && !p.name.toLowerCase().includes(search)) return false;
    return true;
  });

  if (!rows.length) {
    tableBody.innerHTML = `<tr><td colspan="6" class="admin-empty">No products match. Try "+ Add Product".</td></tr>`;
    return;
  }

  tableBody.innerHTML = rows
    .map((p) => {
      const thumb = (p.images && p.images[0] && p.images[0].url) || "";
      return `
        <tr data-id="${p.id}">
          <td>${thumb ? `<img class="admin-thumb" src="${thumb}" alt="">` : ""}</td>
          <td>${escapeHTML(p.name)}</td>
          <td><span class="admin-cat-pill">${p.category}</span></td>
          <td>${formatBirr(p.price)}${p.oldPrice ? ` <s style="opacity:.5">${formatBirr(p.oldPrice)}</s>` : ""}</td>
          <td>${p.featured ? '<span class="admin-featured-dot">●</span>' : ""}</td>
          <td>
            <div class="admin-row-actions">
              <button type="button" data-action="edit">Edit</button>
              <button type="button" data-action="delete">Delete</button>
            </div>
          </td>
        </tr>`;
    })
    .join("");
}

function escapeHTML(str) {
  const div = document.createElement("div");
  div.textContent = str;
  return div.innerHTML;
}

categoryFilter.addEventListener("change", renderTable);
tableSearch.addEventListener("input", renderTable);

tableBody.addEventListener("click", (e) => {
  const btn = e.target.closest("button[data-action]");
  if (!btn) return;
  const id = btn.closest("tr").dataset.id;
  const product = allProducts.find((p) => p.id === id);
  if (btn.dataset.action === "edit") openEditor(product);
  if (btn.dataset.action === "delete") deleteProduct(product);
});

// ---------- Image rows in the editor ----------
function addImageRow(url = "", color = "") {
  const row = document.createElement("div");
  row.className = "admin-image-row";
  row.innerHTML = `
    <input type="text" class="img-url" placeholder="../Pictures/Hoodie/hoodie1.jpg" value="${escapeHTML(url)}">
    <input type="text" class="img-color" placeholder="#000000 (swatch only)" value="${escapeHTML(color || "")}">
    <button type="button" class="admin-image-remove">Remove</button>
  `;
  row.querySelector(".admin-image-remove").addEventListener("click", () => row.remove());
  imagesList.appendChild(row);
}

addImageBtn.addEventListener("click", () => addImageRow());

function collectImages() {
  return [...imagesList.querySelectorAll(".admin-image-row")]
    .map((row) => ({
      url: row.querySelector(".img-url").value.trim(),
      color: row.querySelector(".img-color").value.trim() || null,
    }))
    .filter((img) => img.url);
}

// ---------- Size/stock rows in the editor ----------
function addSizeRow(size = "", stock = "") {
  const row = document.createElement("div");
  row.className = "admin-size-row";
  row.innerHTML = `
    <input type="text" class="size-label-input" placeholder="e.g. M or 42" value="${escapeHTML(size)}">
    <input type="number" class="size-stock-input" min="0" step="1" placeholder="Stock" value="${escapeHTML(String(stock))}">
    <button type="button" class="admin-image-remove">Remove</button>
  `;
  row.querySelector(".admin-image-remove").addEventListener("click", () => row.remove());
  sizesList.appendChild(row);
}

addSizeBtn.addEventListener("click", () => addSizeRow());

function collectSizes() {
  return [...sizesList.querySelectorAll(".admin-size-row")]
    .map((row) => ({
      size: row.querySelector(".size-label-input").value.trim(),
      stock: Number(row.querySelector(".size-stock-input").value) || 0,
    }))
    .filter((s) => s.size);
}

// ---------- Editor open/close ----------
function openEditor(product) {
  formError.textContent = "";
  productForm.reset();
  imagesList.innerHTML = "";
  sizesList.innerHTML = "";

  if (product) {
    editingId = product.id;
    editorTitle.textContent = "Edit Product";
    deleteProductBtn.classList.remove("hidden");
    document.getElementById("fieldName").value = product.name;
    document.getElementById("fieldCategory").value = product.category;
    document.getElementById("fieldType").value = product.type || "simple";
    document.getElementById("fieldPrice").value = product.price;
    document.getElementById("fieldOldPrice").value = product.oldPrice || "";
    fieldDiscountPercent.value = product.discountPercent || "";
    document.getElementById("fieldFeatured").checked = !!product.featured;
    (product.images || []).forEach((img) => addImageRow(img.url, img.color));
    (product.sizes || []).forEach((s) => addSizeRow(s.size, s.stock));
  } else {
    editingId = null;
    editorTitle.textContent = "Add Product";
    deleteProductBtn.classList.add("hidden");
    addImageRow();
  }

  updateDiscountPercentVisibility();
  editorOverlay.classList.remove("hidden");
}

// The manual "% off" badge only makes sense once the real price-vs-oldPrice
// math *can't* produce a genuine discount — otherwise the real math wins
// (see productCardHTML in products-api.js), so hide the field rather than
// let the admin fill in something that'll be silently ignored.
function updateDiscountPercentVisibility() {
  const price = Number(fieldPrice.value);
  const oldPrice = fieldOldPrice.value ? Number(fieldOldPrice.value) : null;
  const hasRealDiscount = !!(oldPrice && price > 0 && oldPrice > price);
  discountPercentRow.classList.toggle("hidden", hasRealDiscount);
  if (hasRealDiscount) fieldDiscountPercent.value = "";
}
fieldPrice.addEventListener("input", updateDiscountPercentVisibility);
fieldOldPrice.addEventListener("input", updateDiscountPercentVisibility);

function closeEditor() {
  editorOverlay.classList.add("hidden");
  editingId = null;
}

newProductBtn.addEventListener("click", () => openEditor(null));
closeEditorBtn.addEventListener("click", closeEditor);
cancelEditorBtn.addEventListener("click", closeEditor);
editorOverlay.addEventListener("click", (e) => {
  if (e.target === editorOverlay) closeEditor();
});

// ---------- Save / delete ----------
productForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  formError.textContent = "";

  const payload = {
    name: document.getElementById("fieldName").value.trim(),
    category: document.getElementById("fieldCategory").value,
    type: document.getElementById("fieldType").value,
    price: Number(document.getElementById("fieldPrice").value),
    oldPrice: document.getElementById("fieldOldPrice").value
      ? Number(document.getElementById("fieldOldPrice").value)
      : null,
    discountPercent:
      !discountPercentRow.classList.contains("hidden") && fieldDiscountPercent.value
        ? Number(fieldDiscountPercent.value)
        : null,
    featured: document.getElementById("fieldFeatured").checked,
    images: collectImages(),
    sizes: collectSizes(),
  };

  if (!payload.images.length) {
    formError.textContent = "Add at least one image.";
    return;
  }

  const saveBtn = document.getElementById("saveProductBtn");
  saveBtn.disabled = true;
  saveBtn.textContent = "Saving…";

  try {
    if (editingId) {
      await apiFetch(`/products/${editingId}`, { method: "PUT", body: JSON.stringify(payload) });
      adminStatus.textContent = `Saved "${payload.name}".`;
    } else {
      await apiFetch("/products", { method: "POST", body: JSON.stringify(payload) });
      adminStatus.textContent = `Added "${payload.name}".`;
    }
    closeEditor();
    await loadProducts();
  } catch (err) {
    formError.textContent = err.message;
  } finally {
    saveBtn.disabled = false;
    saveBtn.textContent = "Save";
  }
});

deleteProductBtn.addEventListener("click", () => {
  if (!editingId) return;
  const product = allProducts.find((p) => p.id === editingId);
  deleteProduct(product);
});

async function deleteProduct(product) {
  if (!product) return;
  if (!confirm(`Delete "${product.name}"? This can't be undone.`)) return;
  try {
    await apiFetch(`/products/${product.id}`, { method: "DELETE" });
    adminStatus.textContent = `Deleted "${product.name}".`;
    closeEditor();
    await loadProducts();
  } catch (err) {
    formError.textContent = err.message;
    adminStatus.textContent = "";
  }
}

// ---------- Init ----------
if (getToken()) {
  showDashboard();
} else {
  showLogin();
}
