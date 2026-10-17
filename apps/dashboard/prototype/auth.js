// Supabase manages session persistence, refresh, and confirmation-link callbacks.
// Authorization is enforced by the backend, never by the visibility of the UI.
export function setupAuth({endpoint, onUserChange}) {
  const el = id => document.getElementById(id);
  let client, backend, userId, generation = 0;
  const pending = new Set();
  const message = (text, error = false) => {
    el('auth-message').textContent = text;
    el('auth-message').className = error ? 'err' : 'muted';
  };
  function render(session) {
    const nextId = session?.user?.id ?? null;
    if (nextId !== userId) {
      generation++;
      pending.forEach(controller => controller.abort());
      pending.clear();
      userId = nextId;
      onUserChange(session);
    }
    const signedIn = Boolean(nextId);
    el('workspace').hidden = !signedIn;
    el('workspace').inert = !signedIn;
    el('auth-form').hidden = signedIn;
    el('sign-out').hidden = !signedIn;
    endpoint.readOnly = signedIn;
    el('account-status').textContent = signedIn
      ? `Signed in as ${session.user.email}` : 'Sign in to use your agents.';
  }
  async function connect() {
    el('auth-retry').hidden = true;
    el('auth-form').hidden = true;
    el('account-status').textContent = 'Connecting…';
    message('');
    try {
      const url = new URL(endpoint.value.trim());
      if (url.protocol !== 'https:' && !(url.protocol === 'http:' &&
          ['localhost', '127.0.0.1', '[::1]'].includes(url.hostname))) {
        throw new Error('Use an HTTPS backend, or localhost for development.');
      }
      if (url.username || url.password || url.search || url.hash) throw new Error('Invalid backend URL.');
      backend = url.href.replace(/\/$/, '');
      const response = await fetch(backend + '/v1/auth/config', {
        redirect: 'error', signal: AbortSignal.timeout(10000),
      });
      if (!response.ok) throw new Error('Could not load sign-in settings.');
      const config = await response.json();
      const {createClient} = await import('https://cdn.jsdelivr.net/npm/@supabase/supabase-js@2.49.1/+esm');
      client = createClient(config.supabase_url, config.supabase_publishable_key, {
        auth: {persistSession: true, autoRefreshToken: true, detectSessionInUrl: true},
      });
      client.auth.onAuthStateChange((_event, session) => render(session));
      const {data, error} = await client.auth.getSession();
      if (error) throw error;
      render(data.session);
    } catch (error) {
      el('account-status').textContent = 'Sign-in unavailable';
      message(error.message || 'Could not connect. Check the backend and try again.', true);
      el('auth-retry').hidden = false;
    }
  }
  el('auth-retry').onclick = () => location.reload();
  // Reload before using another backend so credentials never follow a changed URL.
  const saveEndpoint = endpoint.onchange;
  endpoint.onchange = () => {saveEndpoint?.(); location.reload();};
  el('auth-form').onsubmit = async event => {
    event.preventDefault();
    const signup = event.submitter?.id === 'sign-up';
    const buttons = [el('sign-in'), el('sign-up')];
    buttons.forEach(button => button.disabled = true);
    message(signup ? 'Creating account…' : 'Signing in…');
    try {
      const credentials = {email: el('email').value.trim(), password: el('password').value};
      const {data, error} = signup
        ? await client.auth.signUp({...credentials, options: {emailRedirectTo: location.origin + location.pathname}})
        : await client.auth.signInWithPassword(credentials);
      if (error) throw error;
      el('password').value = '';
      render(data.session);
      message(signup && !data.session ? 'Check your email to confirm your account, then sign in.' : '');
    } catch (error) {
      message(error.message || 'Sign-in failed. Please try again.', true);
    } finally {
      buttons.forEach(button => button.disabled = false);
    }
  };
  el('sign-out').onclick = async () => {
    el('sign-out').disabled = true;
    try {
      const {error} = await client.auth.signOut({scope: 'local'});
      if (error) throw error;
      render(null);
      message('Signed out.');
    } catch (error) {
      message(error.message || 'Could not sign out. Please try again.', true);
    } finally {el('sign-out').disabled = false;}
  };
  connect();
  return {
    async request(path, options = {}) {
      if (!client || !userId) throw new Error('Sign in to continue.');
      if (endpoint.value.trim().replace(/\/$/, '') !== backend) throw new Error('Backend changed. Reload to reconnect.');
      const started = generation;
      const {data, error} = await client.auth.getSession();
      if (error) throw error;
      if (!data.session || started !== generation) {
        if (!data.session) render(null);
        throw new Error('Your session ended. Sign in again.');
      }
      const controller = new AbortController();
      pending.add(controller);
      let streaming = false;
      try {
        const response = await fetch(backend + path, {
          ...options, redirect: 'error', signal: options.signal
            ? AbortSignal.any([controller.signal, options.signal]) : controller.signal,
          headers: {...options.headers, 'Content-Type': 'application/json',
            Authorization: `Bearer ${data.session.access_token}`},
        });
        if (response.status === 401) {
          render(null);
          message('Your session expired. Sign in again.', true);
          throw new Error('Your session expired. Sign in again.');
        }
        if (started !== generation) throw new Error('Your session changed. Try again.');
        streaming = response.ok && response.headers.get('content-type')?.includes('text/event-stream');
        response.authComplete = () => pending.delete(controller);
        const readJSON = response.json.bind(response);
        response.json = async () => {
          const value = await readJSON();
          if (started !== generation) throw new Error('Your session changed. Try again.');
          return value;
        };
        return response;
      } finally {if (!streaming) pending.delete(controller);}
    },
  };
}
