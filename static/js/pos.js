(() => {
  const form = document.getElementById("pos-form");
  if (!form) return;

  const catalogUrl = form.dataset.catalogUrl;
  const unitsUrlTemplate = form.dataset.unitsUrlTemplate;
  const grid = document.getElementById("product-grid");
  const searchInput = document.getElementById("product-search");
  const categorySelect = document.getElementById("category-filter");
  const catalogCount = document.getElementById("catalog-count");
  const catalogError = document.getElementById("catalog-error");
  const retryButton = document.getElementById("catalog-retry");
  const cartElement = document.getElementById("cart-items");
  const cartEmpty = document.getElementById("cart-empty");
  const cartItemCount = document.getElementById("cart-item-count");
  const subtotalElement = document.getElementById("pos-subtotal");
  const totalElement = document.getElementById("pos-total");
  const checkoutButton = document.getElementById("checkout-button");
  const submitAmount = document.getElementById("id_payment_amount");
  const saleType = document.getElementById("id_sale_type");
  const paymentMethod = document.getElementById("id_payment_method");
  const paymentFields = document.getElementById("payment-fields");
  const momoField = document.getElementById("momo-reference-field");
  const momoReference = document.getElementById("id_momo_reference");
  const checkoutNote = document.getElementById("checkout-note");
  const customerSelect = document.getElementById("id_customer");
  const customerSearch = document.getElementById("customer-search");
  const selectedCustomer = document.getElementById("selected-customer");
  const newCustomerToggle = document.getElementById("new-customer-toggle");
  const newCustomerFields = document.getElementById("new-customer-fields");
  const newCustomerCancel = document.getElementById("new-customer-cancel");
  const managementTotal = document.getElementById("id_items-TOTAL_FORMS");

  const products = new Map();
  const cart = new Map();
  let loading = false;
  let requestNumber = 0;
  let searchTimer;
  let catalogItems = [];
  const isManager = form.dataset.isManager === "true";

  const moneyText = (cents) => {
    const whole = Math.floor(cents / 100).toLocaleString("en-GH");
    const fraction = String(cents % 100).padStart(2, "0");
    return `GH₵${whole}.${fraction}`;
  };
  const priceCents = (price) => {
    const [whole = "0", fraction = ""] = String(price).split(".");
    return Number(whole) * 100 + Number(fraction.padEnd(2, "0").slice(0, 2));
  };
  const totalCents = () => [...cart.values()].reduce((sum, item) => {
    const quantity = item.product.track_individual_units ? item.unitIdentifiers.length : item.quantity;
    return sum + priceCents(item.product.selling_price) * quantity;
  }, 0);

  function setCatalogLoading(isLoading) {
    loading = isLoading;
    grid.setAttribute("aria-busy", String(isLoading));
    updateCheckoutState();
    if (isLoading) {
      grid.replaceChildren();
      const loadingMessage = document.createElement("div");
      loadingMessage.className = "pos-loading";
      loadingMessage.textContent = "Loading catalogue…";
      grid.appendChild(loadingMessage);
      catalogError.hidden = true;
      retryButton.hidden = true;
    }
  }

  async function loadCatalog() {
    const thisRequest = ++requestNumber;
    const params = new URLSearchParams();
    if (searchInput.value.trim()) params.set("q", searchInput.value.trim());
    if (categorySelect.value) params.set("category", categorySelect.value);
    setCatalogLoading(true);
    try {
      const response = await fetch(`${catalogUrl}?${params}`, { headers: { Accept: "application/json" } });
      if (!response.ok) throw new Error("Catalogue request failed");
      const data = await response.json();
      if (thisRequest !== requestNumber) return;
      catalogItems = data.products;
      products.clear();
      catalogItems.forEach((product) => products.set(product.id, product));
      populateCategories(data.categories);
      renderProducts();
    } catch (error) {
      if (thisRequest !== requestNumber) return;
      setCatalogLoading(false);
      grid.replaceChildren();
      catalogCount.textContent = "Catalogue unavailable";
      catalogError.hidden = false;
      retryButton.hidden = false;
      updateCheckoutState();
    }
  }

  function populateCategories(categories) {
    const current = categorySelect.value;
    const options = [new Option("All categories", "")];
    categories.forEach((category) => options.push(new Option(category.name, category.id)));
    categorySelect.replaceChildren(...options);
    if (categories.some((category) => String(category.id) === current)) categorySelect.value = current;
  }

  function renderProducts() {
    setCatalogLoading(false);
    grid.replaceChildren();
    catalogCount.textContent = `${catalogItems.length} product${catalogItems.length === 1 ? "" : "s"} found`;
    if (!catalogItems.length) {
      const empty = document.createElement("div");
      empty.className = "pos-catalog-empty";
      empty.innerHTML = '<span class="empty-icon"><i class="bi bi-search"></i></span><strong>No matching products</strong><p>Try a different name, SKU or category.</p>';
      grid.appendChild(empty);
      updateCheckoutState();
      return;
    }

    catalogItems.forEach((product) => {
      const card = document.createElement("article");
      card.className = "pos-product-card";
      const icon = document.createElement("div");
      icon.className = "pos-product-icon";
      icon.innerHTML = '<i class="bi bi-box-seam" aria-hidden="true"></i>';
      icon.setAttribute("role", "img");
      icon.setAttribute("aria-label", "Product image placeholder");
      const category = document.createElement("span");
      category.className = "pos-product-category";
      category.textContent = product.category;
      const name = document.createElement("h3");
      name.textContent = product.name;
      const sku = document.createElement("span");
      sku.className = "pos-product-sku";
      sku.textContent = `${product.sku}${product.model ? ` · ${product.model}` : ""}`;
      const price = document.createElement("strong");
      price.className = "pos-product-price";
      price.textContent = moneyText(priceCents(product.selling_price));
      const stock = document.createElement("span");
      stock.className = `pos-stock-label${product.stock_quantity === 0 ? " is-out" : product.is_low_stock ? " is-low" : ""}`;
      stock.setAttribute("aria-label", product.stock_quantity === 0 ? "Out of stock" : product.is_low_stock ? "Low stock" : "In stock");
      stock.textContent = product.stock_quantity === 0
        ? "Out of stock"
        : `${product.stock_quantity} available${product.is_low_stock ? " · Low stock" : ""}`;
      const add = document.createElement("button");
      add.type = "button";
      add.className = "pos-add-button";
      add.disabled = product.stock_quantity < 1 || (cart.size >= 12 && !cart.has(product.id));
      add.setAttribute("aria-label", product.track_individual_units ? `Choose a unit of ${product.name}` : `Add ${product.name} to sale`);
      add.innerHTML = product.track_individual_units ? '<i class="bi bi-upc-scan"></i><span>Choose unit</span>' : '<i class="bi bi-plus-lg"></i><span>Add</span>';
      add.addEventListener("click", () => addProduct(product));
      card.append(icon, category, name, sku, price, stock, add);
      grid.appendChild(card);
    });
    updateCheckoutState();
  }

  async function addProduct(product) {
    if (product.stock_quantity < 1) return;
    const existing = cart.get(product.id);
    if (product.track_individual_units) {
      if (existing) {
        document.getElementById(`cart-product-${product.id}`)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
        return;
      }
      if (cart.size >= 12) return;
      cart.set(product.id, { product, quantity: 0, unitIdentifiers: [], availableUnits: [], unitsLoading: true, unitsError: false });
      renderCart();
      try {
        const url = unitsUrlTemplate.replace("/0/units/", `/${product.id}/units/`);
        const response = await fetch(url, { headers: { Accept: "application/json" } });
        if (!response.ok) throw new Error("Unit request failed");
        const data = await response.json();
        const item = cart.get(product.id);
        if (!item) return;
        item.availableUnits = data.units.map((unit) => unit.identifier);
        item.unitIdentifiers = item.unitIdentifiers.filter((identifier) => item.availableUnits.includes(identifier));
        item.unitsLoading = false;
      } catch (error) {
        const item = cart.get(product.id);
        if (item) {
          item.unitsLoading = false;
          item.unitsError = true;
        }
      }
      renderCart();
      return;
    }
    if (existing) {
      if (existing.quantity >= product.stock_quantity) {
        showCartMessage("The cart already contains all available stock for this product.");
        return;
      }
      existing.quantity += 1;
    } else {
      if (cart.size >= 12) {
        showCartMessage("A sale can contain up to 12 different product lines.");
        return;
      }
      cart.set(product.id, { product, quantity: 1, unitIdentifiers: [] });
    }
    renderCart();
  }

  function showCartMessage(message) {
    const previous = document.getElementById("cart-action-message");
    if (previous) previous.remove();
    const note = document.createElement("div");
    note.id = "cart-action-message";
    note.className = "pos-inline-message";
    note.setAttribute("role", "status");
    note.textContent = message;
    cartElement.prepend(note);
  }

  function renderCart() {
    cartElement.replaceChildren();
    const entries = [...cart.entries()];
    cartEmpty.hidden = entries.length > 0;
    if (!entries.length) cartElement.appendChild(cartEmpty);
    let unitCount = 0;
    entries.forEach(([productId, item], index) => {
      const line = document.createElement("article");
      line.className = "pos-cart-line";
      line.id = `cart-product-${productId}`;
      const top = document.createElement("div");
      top.className = "pos-cart-line-top";
      const name = document.createElement("div");
      name.className = "pos-cart-line-name";
      const strong = document.createElement("strong");
      strong.textContent = item.product.name;
      const sku = document.createElement("span");
      sku.textContent = item.product.sku;
      name.append(strong, sku);
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "pos-remove-button";
      remove.setAttribute("aria-label", `Remove ${item.product.name} from cart`);
      remove.innerHTML = '<i class="bi bi-trash3"></i>';
      remove.addEventListener("click", () => {
        cart.delete(productId);
        renderCart();
        renderProducts();
      });
      top.append(name, remove);
      line.appendChild(top);

      let quantity = item.quantity;
      if (item.product.track_individual_units) {
        const unitSection = document.createElement("div");
        unitSection.className = "pos-unit-selector";
        const label = document.createElement("span");
        label.className = "pos-unit-heading";
        label.textContent = item.unitsLoading ? "Loading available serial numbers…" : "Select available serial number / IMEI";
        unitSection.appendChild(label);
        if (item.unitsError) {
          const error = document.createElement("span");
          error.className = "pos-inline-error";
          error.textContent = "Serial numbers could not be loaded. Remove and try again.";
          unitSection.appendChild(error);
        } else if (!item.unitsLoading && item.availableUnits.length) {
          const list = document.createElement("div");
          list.className = "pos-unit-list";
          item.availableUnits.forEach((identifier) => {
            const option = document.createElement("label");
            option.className = "pos-unit-option";
            const checkbox = document.createElement("input");
            checkbox.type = "checkbox";
            checkbox.value = identifier;
            checkbox.checked = item.unitIdentifiers.includes(identifier);
            checkbox.disabled = !checkbox.checked && item.unitIdentifiers.length >= item.product.stock_quantity;
            checkbox.addEventListener("change", () => {
              item.unitIdentifiers = [...list.querySelectorAll("input:checked")].map((selected) => selected.value);
              renderCart();
            });
            const text = document.createElement("span");
            text.textContent = identifier;
            option.append(checkbox, text);
            list.appendChild(option);
          });
          unitSection.appendChild(list);
          quantity = item.unitIdentifiers.length;
        } else if (!item.unitsLoading) {
          const empty = document.createElement("span");
          empty.className = "pos-inline-error";
          empty.textContent = "No available serial numbers remain for this product.";
          unitSection.appendChild(empty);
        }
        line.appendChild(unitSection);
      } else {
        const controls = document.createElement("div");
        controls.className = "pos-quantity-row";
        const quantityControls = document.createElement("div");
        quantityControls.className = "pos-quantity-control";
        const decrease = document.createElement("button");
        decrease.type = "button";
        decrease.textContent = "−";
        decrease.setAttribute("aria-label", `Decrease ${item.product.name} quantity`);
        decrease.disabled = item.quantity <= 1;
        decrease.addEventListener("click", () => { item.quantity -= 1; renderCart(); });
        const quantityValue = document.createElement("span");
        quantityValue.textContent = String(item.quantity);
        const increase = document.createElement("button");
        increase.type = "button";
        increase.textContent = "+";
        increase.setAttribute("aria-label", `Increase ${item.product.name} quantity`);
        increase.disabled = item.quantity >= item.product.stock_quantity;
        increase.addEventListener("click", () => {
          if (item.quantity >= item.product.stock_quantity) return;
          item.quantity += 1;
          renderCart();
        });
        quantityControls.append(decrease, quantityValue, increase);
        const lineTotal = document.createElement("strong");
        lineTotal.textContent = moneyText(priceCents(item.product.selling_price) * item.quantity);
        controls.append(quantityControls, lineTotal);
        line.appendChild(controls);
      }

      if (item.product.track_individual_units) {
        const lineTotal = document.createElement("div");
        lineTotal.className = "pos-serial-line-total";
        lineTotal.textContent = `${quantity} selected · ${moneyText(priceCents(item.product.selling_price) * quantity)}`;
        line.appendChild(lineTotal);
      }
      unitCount += quantity;
      const hiddenProduct = document.createElement("input");
      hiddenProduct.type = "hidden";
      hiddenProduct.name = `items-${index}-product`;
      hiddenProduct.value = String(productId);
      const hiddenQuantity = document.createElement("input");
      hiddenQuantity.type = "hidden";
      hiddenQuantity.name = `items-${index}-quantity`;
      hiddenQuantity.value = String(quantity);
      const hiddenUnits = document.createElement("textarea");
      hiddenUnits.hidden = true;
      hiddenUnits.name = `items-${index}-unit_identifiers`;
      hiddenUnits.value = item.unitIdentifiers.join("\n");
      line.append(hiddenProduct, hiddenQuantity, hiddenUnits);
      cartElement.appendChild(line);
    });

    cartItemCount.textContent = `${unitCount} item${unitCount === 1 ? "" : "s"}`;
    const total = totalCents();
    subtotalElement.textContent = moneyText(total);
    totalElement.textContent = moneyText(total);
    if (managementTotal) managementTotal.value = String(entries.length);
    updateCheckoutState();
    renderProducts();
  }

  function updateCheckoutState() {
    const hasIncompleteSerializedLine = [...cart.values()].some((item) => item.product.track_individual_units && (
      item.unitsLoading || item.unitsError || item.unitIdentifiers.length < 1 || item.unitIdentifiers.length > item.product.stock_quantity
    ));
    const customerChosen = Boolean(customerSelect.value || (
      document.getElementById("id_new_customer_name").value.trim()
      && document.getElementById("id_new_customer_phone").value.trim()
    ));
    const creditNeedsCustomer = saleType.value === "CREDIT" && !customerChosen;
    const amount = totalCents();
    const momoNeedsReference = saleType.value === "CASH" && paymentMethod.value === "MTN_MOMO" && !momoReference.value.trim();
    checkoutButton.disabled = loading || cart.size === 0 || amount <= 0 || hasIncompleteSerializedLine || creditNeedsCustomer || momoNeedsReference;
    submitAmount.value = saleType.value === "CASH" && amount > 0 ? `${Math.floor(amount / 100)}.${String(amount % 100).padStart(2, "0")}` : "";

    paymentFields.hidden = saleType.value !== "CASH";
    momoField.hidden = saleType.value !== "CASH" || paymentMethod.value !== "MTN_MOMO";
    if (saleType.value !== "CASH" || paymentMethod.value !== "MTN_MOMO") momoReference.value = "";
    checkoutNote.className = "pos-checkout-note";
    if (saleType.value === "CREDIT" && !isManager) {
      checkoutNote.textContent = "This employee-created credit sale will be marked PENDING APPROVAL. Stock is deducted only after an owner or secretary approves it.";
      checkoutNote.classList.add("is-warning");
    } else if (saleType.value === "CREDIT") {
      checkoutNote.textContent = "A credit sale created by an owner or secretary is approved through the existing manager workflow.";
      checkoutNote.classList.add("is-info");
    } else if (paymentMethod.value === "MTN_MOMO") {
      checkoutNote.textContent = "MTN MoMo will remain PENDING CONFIRMATION until an owner or secretary verifies it.";
      checkoutNote.classList.add("is-warning");
    } else {
      checkoutNote.textContent = "Cash sales must be paid in full at checkout.";
      checkoutNote.classList.add("is-info");
    }
  }

  function updateCustomerFilter() {
    const term = customerSearch.value.trim().toLocaleLowerCase();
    [...customerSelect.options].forEach((option, index) => {
      if (index === 0 || !term) {
        option.hidden = false;
        return;
      }
      option.hidden = !option.text.toLocaleLowerCase().includes(term);
    });
  }

  function updateSelectedCustomer() {
    const option = customerSelect.selectedOptions[0];
    selectedCustomer.textContent = customerSelect.value ? `Selected: ${option.text}` : "";
    updateCheckoutState();
  }

  searchInput.addEventListener("input", () => {
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(loadCatalog, 220);
  });
  categorySelect.addEventListener("change", loadCatalog);
  retryButton.addEventListener("click", loadCatalog);
  saleType.addEventListener("change", updateCheckoutState);
  paymentMethod.addEventListener("change", updateCheckoutState);
  momoReference.addEventListener("input", updateCheckoutState);
  customerSelect.addEventListener("change", updateSelectedCustomer);
  customerSearch.addEventListener("input", updateCustomerFilter);
  newCustomerToggle.addEventListener("click", () => {
    const opening = newCustomerFields.hidden;
    newCustomerFields.hidden = !opening;
    newCustomerToggle.setAttribute("aria-expanded", String(opening));
    customerSelect.disabled = opening;
    customerSearch.disabled = opening;
    if (opening) {
      customerSelect.value = "";
      customerSearch.value = "";
      updateSelectedCustomer();
    }
  });
  newCustomerCancel.addEventListener("click", () => {
    newCustomerFields.hidden = true;
    newCustomerToggle.setAttribute("aria-expanded", "false");
    customerSelect.disabled = false;
    customerSearch.disabled = false;
    ["id_new_customer_name", "id_new_customer_phone", "id_new_customer_email", "id_new_customer_address"].forEach((id) => {
      const field = document.getElementById(id);
      if (field) field.value = "";
    });
    updateCheckoutState();
  });
  ["id_new_customer_name", "id_new_customer_phone"].forEach((id) => {
    document.getElementById(id).addEventListener("input", updateCheckoutState);
  });
  form.addEventListener("submit", (event) => {
    updateCheckoutState();
    if (checkoutButton.disabled) {
      event.preventDefault();
      showCartMessage("Complete the cart, customer and payment details before checkout.");
      return;
    }
    checkoutButton.disabled = true;
    checkoutButton.querySelector("span").textContent = "Processing…";
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "/" && !["INPUT", "TEXTAREA", "SELECT"].includes(document.activeElement.tagName)) {
      event.preventDefault();
      searchInput.focus();
    }
  });

  function restoreCart(initialLines) {
    initialLines.forEach((line) => {
      const product = products.get(Number(line.product_id));
      if (!product) return;
      cart.set(product.id, {
        product,
        quantity: Number(line.quantity) || 1,
        unitIdentifiers: line.unit_identifiers || [],
        availableUnits: [],
        unitsLoading: product.track_individual_units,
        unitsError: false,
      });
      if (product.track_individual_units) {
        const item = cart.get(product.id);
        const url = unitsUrlTemplate.replace("/0/units/", `/${product.id}/units/`);
        fetch(url, { headers: { Accept: "application/json" } })
          .then((response) => { if (!response.ok) throw new Error("Unit request failed"); return response.json(); })
          .then((data) => {
            item.availableUnits = data.units.map((unit) => unit.identifier);
            item.unitIdentifiers = item.unitIdentifiers.filter((identifier) => item.availableUnits.includes(identifier));
            item.unitsLoading = false;
            renderCart();
          })
          .catch(() => { item.unitsLoading = false; item.unitsError = true; renderCart(); });
      }
    });
    renderCart();
  }

  async function initialize() {
    await loadCatalog();
    const initialElement = document.getElementById("pos-cart-initial");
    try {
      const initialLines = initialElement ? JSON.parse(initialElement.textContent) : [];
      restoreCart(initialLines);
    } catch (error) {
      showCartMessage("The previous cart could not be restored. Please review the form errors and add products again.");
    }
    updateSelectedCustomer();
    updateCheckoutState();
  }

  initialize();
})();
