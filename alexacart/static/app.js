function selectProduct(index, productName, price, imageUrl, productUrl, brand, itemId) {
    var row = document.getElementById('row-' + index);
    if (!row) return;

    // Update hidden form fields
    var nameInput = row.querySelector('.hidden-product-name');
    var urlInput = row.querySelector('.hidden-product-url');
    var imgInput = row.querySelector('.hidden-image-url');
    var brandInput = row.querySelector('.hidden-brand');
    var itemIdInput = row.querySelector('.hidden-item-id');
    if (nameInput) nameInput.value = productName;
    if (urlInput) urlInput.value = productUrl || '';
    if (imgInput) imgInput.value = imageUrl || '';
    if (brandInput) brandInput.value = brand || '';
    if (itemIdInput) itemIdInput.value = itemId || '';

    // Unmark skip if it was set
    var skipInput = row.querySelector('.hidden-skip');
    if (skipInput) skipInput.value = '';
    var skipOpt = row.querySelector('.skip-option');
    if (skipOpt) skipOpt.classList.remove('selected');

    // Deselect all existing options
    row.querySelectorAll('input[type="radio"]').forEach(function(r) { r.checked = false; });
    row.querySelectorAll('.product-option').forEach(function(opt) { opt.classList.remove('selected'); });

    // Remove any previously added custom option
    var prev = row.querySelector('.custom-product-option');
    if (prev) prev.remove();

    // Find or create the product-options container
    var optionsDiv = row.querySelector('.product-options');
    if (!optionsDiv) {
        optionsDiv = document.createElement('div');
        optionsDiv.className = 'product-options';
        // Insert before the custom-url-section or skip section
        var urlSection = row.querySelector('.custom-url-section');
        var cell = row.querySelector('.product-cell');
        if (urlSection) {
            cell.insertBefore(optionsDiv, urlSection);
        } else {
            cell.appendChild(optionsDiv);
        }
    }

    // Build a new product-option label that matches the existing style
    var label = document.createElement('label');
    label.className = 'product-option selected custom-product-option';

    var radio = document.createElement('input');
    radio.type = 'radio';
    radio.name = 'items[' + index + '][selected]';
    radio.value = 'custom';
    radio.checked = true;
    radio.setAttribute('data-product-name', productName);
    radio.setAttribute('data-product-url', productUrl || '');
    radio.setAttribute('data-image-url', imageUrl || '');
    radio.setAttribute('data-brand', brand || '');
    radio.setAttribute('data-item-id', itemId || '');
    radio.onchange = function() { selectAlternative(index, this); };
    label.appendChild(radio);

    if (imageUrl) {
        var img = document.createElement('img');
        img.src = imageUrl;
        img.alt = '';
        img.className = 'option-thumb';
        label.appendChild(img);
    }

    var details = document.createElement('span');
    details.className = 'option-details';
    var strong = document.createElement('strong');
    strong.textContent = productName;
    details.appendChild(strong);
    if (brand) {
        var brandSpan = document.createElement('span');
        brandSpan.className = 'brand';
        brandSpan.textContent = brand;
        details.appendChild(brandSpan);
    }
    if (price) {
        var priceSpan = document.createElement('span');
        priceSpan.className = 'price';
        priceSpan.textContent = price;
        details.appendChild(priceSpan);
    }
    if (productUrl) {
        var link = document.createElement('a');
        link.href = productUrl;
        link.target = '_blank';
        link.className = 'product-link';
        link.textContent = 'view';
        link.onclick = function(e) { e.stopPropagation(); };
        details.appendChild(link);
    }
    label.appendChild(details);

    optionsDiv.appendChild(label);

    // Update status badge
    var badge = row.querySelector('.badge');
    if (badge) {
        badge.className = 'badge badge-matched';
        badge.textContent = 'Selected';
    }
}

function selectAlternative(index, radio) {
    var row = document.getElementById('row-' + index);
    if (!row) return;

    // Update hidden fields from radio data attributes
    var nameInput = row.querySelector('.hidden-product-name');
    var urlInput = row.querySelector('.hidden-product-url');
    var imgInput = row.querySelector('.hidden-image-url');
    var brandInput = row.querySelector('.hidden-brand');

    var itemIdInput = row.querySelector('.hidden-item-id');
    if (nameInput) nameInput.value = radio.dataset.productName || '';
    if (urlInput) urlInput.value = radio.dataset.productUrl || '';
    if (imgInput) imgInput.value = radio.dataset.imageUrl || '';
    if (brandInput) brandInput.value = radio.dataset.brand || '';
    if (itemIdInput) itemIdInput.value = radio.dataset.itemId || '';

    // Unmark skip
    var skipInput = row.querySelector('.hidden-skip');
    if (skipInput) skipInput.value = '';
    var skipOpt = row.querySelector('.skip-option');
    if (skipOpt) skipOpt.classList.remove('selected');

    // Highlight selected option
    row.querySelectorAll('.product-option').forEach(function(opt) { opt.classList.remove('selected'); });
    radio.closest('.product-option').classList.add('selected');

    // Update status badge
    var badge = row.querySelector('.badge');
    if (badge) {
        if (radio.value === '0') {
            // Restoring original proposed product — restore original status
            badge.className = 'badge ' + (badge.dataset.originalClass || 'badge-matched');
            badge.textContent = badge.dataset.originalStatus || 'Selected';
        } else {
            badge.className = 'badge badge-matched';
            badge.textContent = 'Selected';
        }
    }
}

function toggleSkip(index) {
    var row = document.getElementById('row-' + index);
    if (!row) return;

    var skipInput = row.querySelector('.hidden-skip');
    if (skipInput) skipInput.value = '1';

    // Clear the hidden product name so commit skips this item
    var nameInput = row.querySelector('.hidden-product-name');
    if (nameInput) nameInput.value = '';

    // Highlight skip option, deselect product options
    row.querySelectorAll('.product-option').forEach(function(opt) { opt.classList.remove('selected'); });
    var skipOpt = row.querySelector('.skip-option');
    if (skipOpt) skipOpt.classList.add('selected');

    var badge = row.querySelector('.badge');
    if (badge) {
        badge.className = 'badge badge-substituted';
        badge.textContent = 'Skipped';
    }
}

function fetchProductUrl(index) {
    var row = document.getElementById('row-' + index);
    if (!row) return;

    var urlInput = row.querySelector('.custom-url-val-' + index);
    if (!urlInput || !urlInput.value) return;

    var resultDiv = document.getElementById('custom-result-' + index);
    var spinner = document.getElementById('url-spinner-' + index);
    var fetchBtn = document.getElementById('fetch-btn-' + index);

    if (fetchBtn) {
        fetchBtn.disabled = true;
        fetchBtn.dataset.origText = fetchBtn.textContent;
        fetchBtn.textContent = 'Fetching\u2026';
    }
    if (spinner) spinner.style.display = 'inline-block';
    if (resultDiv) resultDiv.innerHTML = '';

    var formData = new FormData();
    formData.append('url', urlInput.value);
    formData.append('index', index);
    var sessionInput = document.querySelector('input[name="session_id"]');
    if (sessionInput) formData.append('session_id', sessionInput.value);

    fetch('/order/fetch-url', { method: 'POST', body: formData })
        .then(function(resp) {
            if (!resp.ok) throw new Error('Request failed: ' + resp.status);
            return resp.text();
        })
        .then(function(html) {
            if (resultDiv) resultDiv.innerHTML = html;
            // Execute any <script> tags in the response
            var scripts = resultDiv.querySelectorAll('script');
            scripts.forEach(function(script) {
                var newScript = document.createElement('script');
                newScript.textContent = script.textContent;
                document.head.appendChild(newScript).parentNode.removeChild(newScript);
            });
        })
        .catch(function(err) {
            if (resultDiv) {
                resultDiv.innerHTML = '<div class="status-message status-error" style="margin-top:0.5rem">Error: ' + err.message + '</div>';
            }
        })
        .finally(function() {
            if (spinner) spinner.style.display = 'none';
            if (fetchBtn) {
                fetchBtn.disabled = false;
                fetchBtn.textContent = fetchBtn.dataset.origText || 'Fetch';
            }
        });
}
