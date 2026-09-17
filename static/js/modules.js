(() => {
  const body = document.getElementById('moduleBody');
  const addForm = document.getElementById('addForm');
  if (!body || !addForm) return;
  const search = document.getElementById('searchModules');
  const sort = document.getElementById('sortSelect');
  const toast = (message, type = 'success') => window.showToast(message, type);
  const value = (row, field) => row.querySelector(`[data-field="${field}"]`).value;
  const post = async (url, payload) => {
    const response = await fetch(url, {
      method: 'POST',
      headers: {
        'X-CSRFToken': addForm.querySelector('[name="csrfmiddlewaretoken"]').value,
        'X-Requested-With': 'XMLHttpRequest',
      },
      body: payload,
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok || !data.ok) throw new Error(data.error || 'Unable to save. Please try again.');
    return data;
  };
  const refreshRows = () => {
    const rows = [...body.querySelectorAll('tr[data-id]')];
    const query = search.value.trim().toLowerCase();
    rows.forEach(row => {
      const level = row.querySelector('[data-field="level"]');
      row.hidden = !`${value(row, 'name')} ${level.value} ${level.selectedOptions[0].text}`.toLowerCase().includes(query);
    });
    rows.sort((a, b) => {
      const av = value(a, sort.value);
      const bv = value(b, sort.value);
      if (['credits', 'grade_percent'].includes(sort.value)) {
        return (bv === '' ? -1 : Number(bv)) - (av === '' ? -1 : Number(av));
      }
      return av.localeCompare(bv);
    }).forEach(row => body.appendChild(row));
    let empty = document.getElementById('emptyModules');
    if (!empty) {
      empty = document.createElement('tr');
      empty.id = 'emptyModules';
      const cell = document.createElement('td');
      cell.colSpan = 6;
      cell.className = 'empty-state';
      empty.appendChild(cell);
      body.appendChild(empty);
    }
    empty.hidden = rows.some(row => !row.hidden);
    empty.firstElementChild.textContent = rows.length
      ? 'No modules match your search.' : 'No modules yet. Add your first result above.';
  };
  addForm.addEventListener('submit', async event => {
    event.preventDefault();
    const button = addForm.querySelector('button[type="submit"]');
    button.disabled = true;
    button.textContent = 'Adding…';
    try {
      await post(addForm.action, new FormData(addForm));
      window.location.reload();
    } catch (error) {
      toast(error.message, 'error');
      button.disabled = false;
      button.textContent = 'Add module';
    }
  });
  body.addEventListener('change', async event => {
    const input = event.target.closest('[data-field]');
    if (!input) return;
    const row = input.closest('tr[data-id]');
    const status = row.querySelector('.module-status');
    const controls = [...row.querySelectorAll('input,select,button')];
    controls.forEach(control => { control.disabled = true; });
    status.textContent = 'Saving…';
    try {
      const data = await post(row.dataset.updateUrl, new URLSearchParams({field: input.dataset.field, value: input.value}));
      input.value = data[input.dataset.field] ?? '';
      input.setAttribute('aria-invalid', 'false');
      status.textContent = row.querySelector('[aria-invalid="true"]') ? 'Not saved' : 'Saved';
      refreshRows();
    } catch (error) {
      input.setAttribute('aria-invalid', 'true');
      status.textContent = 'Not saved';
      toast(error.message, 'error');
    } finally {
      controls.forEach(control => { control.disabled = false; });
    }
  });
  body.addEventListener('submit', async event => {
    const form = event.target.closest('.delete-form');
    if (!form) return;
    event.preventDefault();
    if (!window.confirm('Delete this module?')) return;
    const button = form.querySelector('button');
    button.disabled = true;
    try {
      await post(form.action);
      form.closest('tr[data-id]').remove();
      refreshRows();
      toast('Module deleted.');
    } catch (error) {
      toast(error.message, 'error');
    } finally {
      button.disabled = false;
    }
  });
  search.addEventListener('input', refreshRows);
  sort.addEventListener('change', refreshRows);
  refreshRows();
})();
