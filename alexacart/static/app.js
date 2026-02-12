function selectProduct(index, productName, price, imageUrl, productUrl) {
    const row = document.getElementById('row-' + index);
    if (!row) return;

    const nameInput = row.querySelector('.hidden-product-name');
    const urlInput = row.querySelector('.hidden-product-url');
    const priceCell = row.querySelector('.price');

    if (nameInput) nameInput.value = productName;
    if (urlInput) urlInput.value = productUrl || '';
    if (priceCell) priceCell.textContent = price || 'N/A';

    const badge = row.querySelector('.badge');
    if (badge) {
        badge.className = 'badge badge-matched';
        badge.textContent = 'Selected';
    }
}

function selectAlternative(index, radio) {
    const row = document.getElementById('row-' + index);
    if (!row) return;

    // Update hidden fields from radio data attributes
    const nameInput = row.querySelector('.hidden-product-name');
    const urlInput = row.querySelector('.hidden-product-url');
    const priceCell = row.querySelector('.price');

    if (nameInput) nameInput.value = radio.dataset.productName || '';
    if (urlInput) urlInput.value = radio.dataset.productUrl || '';
    if (priceCell) priceCell.textContent = radio.dataset.price || 'N/A';

    // Highlight selected option
    row.querySelectorAll('.product-option').forEach(opt => opt.classList.remove('selected'));
    radio.closest('.product-option').classList.add('selected');
}
