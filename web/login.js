'use strict';
document.getElementById('login-form').addEventListener('submit', async event => {
  event.preventDefault();
  const button = event.target.querySelector('button');
  button.disabled = true;
  const error = document.getElementById('login-error');
  error.textContent = '';
  try {
    const response = await fetch('/api/login', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({password: document.getElementById('password').value})
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || '접속하지 못했습니다.');
    document.getElementById('password').value = '';
    location.replace('/');
  } catch (e) { error.textContent = e.message; }
  finally { button.disabled = false; }
});
