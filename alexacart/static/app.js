function selectProduct(index, productName, price, imageUrl) {
    const row = document.getElementById('row-' + index);
    if (!row) return;

    const nameInput = row.querySelector('input[name$="[product_name]"]');
    const nameSpan = row.querySelector('.product-name span');
    const priceCell = row.querySelector('.price');
    const picker = document.getElementById('picker-' + index);

    if (nameInput) nameInput.value = productName;
    if (nameSpan) nameSpan.textContent = productName;
    if (priceCell) priceCell.textContent = price || 'N/A';
    if (picker) picker.innerHTML = '';

    const badge = row.querySelector('.badge');
    if (badge) {
        badge.className = 'badge badge-matched';
        badge.textContent = 'Selected';
    }
}
