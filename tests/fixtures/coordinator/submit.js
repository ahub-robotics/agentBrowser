document.querySelector('form[data-local-submit]')?.addEventListener('submit', async event => {
  event.preventDefault();
  const form = event.currentTarget;
  const button = form.querySelector('button');
  button.disabled = true;
  const response = await fetch(form.action, {
    method: 'POST', headers: {'Content-Type': 'application/x-www-form-urlencoded'},
    body: new URLSearchParams(new FormData(form))
  });
  const result = await response.json();
  const confirmation = document.querySelector('#confirmation');
  confirmation.textContent = result.accepted ? `Submission confirmed for item ${result.item_id}` : 'Submission rejected';
  confirmation.dataset.accepted = String(result.accepted);
});
