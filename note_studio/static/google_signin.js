'use strict';
document.addEventListener('click', async event => {
  const button = event.target.closest('[data-google-signin-popup]');
  if (!button) return;
  event.preventDefault();
  const popup = window.open('about:blank', 'pw-google-signin', 'width=560,height=740');
  if (!popup) { window.alert('Allow pop-ups for this app, then press Sign in again.'); return; }
  button.disabled = true;
  try {
    const response = await fetch(button.dataset.endpoint, {method:'POST', credentials:'same-origin'});
    const result = await response.json();
    if (!response.ok || !result.url) throw new Error(result.detail || 'Google sign-in could not start. Check the app registration and retry.');
    const url = new URL(result.url);
    if (url.protocol !== 'https:' || url.hostname !== 'accounts.google.com') throw new Error('Unexpected Google sign-in address.');
    popup.location.assign(url.href);
    if (window.htmx) window.htmx.ajax('GET', button.dataset.status, {target:button.dataset.target, swap:'outerHTML'});
  } catch (error) {
    popup.close();
    window.alert(error.message || 'Google sign-in could not start.');
  } finally { button.disabled = false; }
});
