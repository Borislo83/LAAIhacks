export function setupPreferences({request}) {
  const el = id => document.getElementById(id);
  let userId, generation = 0, loaded = false, saving = false;
  function status(text, error = false) {
    el('preferences-status').textContent = text;
    el('preferences-status').className = error ? 'err' : 'muted';
  }
  const LABEL = {price: 'Find the best value', quality: 'Prioritize quality', balanced: 'Balanced'};
  function showLearned(learned) {
    const box = el('learned-priority');
    if (!learned) { box.hidden = true; return; }
    const title = learned.source === 'learned'
      ? `Learned priority in use: ${LABEL[learned.priority]}`
      : learned.source === 'setting' && learned.priority !== 'balanced' && learned.priority !== learned.effective
        ? `Learned: ${LABEL[learned.priority]} — your saved setting wins`
        : learned.priority !== 'balanced'
          ? `Learned: ${LABEL[learned.priority]}`
          : 'No learned priority yet';
    el('learned-title').textContent = title;
    el('learned-reason').textContent = learned.reason;
    box.hidden = false;
  }
  async function loadLearned(path = '/v1/preferences/learned', options) {
    const started = generation;
    try {
      const response = await request(path, options);
      if (!response.ok) throw new Error();
      const learned = await response.json();
      if (started === generation && userId) showLearned(learned);
      return true;
    } catch {
      if (started === generation && userId && !options) showLearned(null);
      return false;
    }
  }
  el('learned-reset').onclick = async () => {
    if (!loaded || !userId) return;
    status('Resetting learning…');
    const ok = await loadLearned('/v1/preferences/learned/reset', {method: 'POST'});
    status(ok ? 'Learning reset. Focal starts again from your next choices.' : 'Could not reset learning. Please try again.', !ok);
  };
  function populate(values) {
    el('display-density').value = values.display_density || 'comfortable';
    el('shopping-priority').value = values.shopping_priority || 'balanced';
    el('proactive-enabled').checked = Boolean(values.proactive_enabled);
    el('user-notes').value = values.notes || '';
    document.body.dataset.density = values.display_density || 'comfortable';
  }
  async function load() {
    const started = generation;
    loaded = false;
    el('preferences-fields').disabled = true;
    el('preferences-retry').hidden = true;
    status('Loading preferences…');
    try {
      const response = await request('/v1/preferences');
      if (!response.ok) throw new Error('Could not load your saved preferences.');
      const values = await response.json();
      if (started !== generation || !userId) return;
      populate(values);
      loaded = true;
      el('preferences-fields').disabled = false;
      status('Preferences loaded.');
      loadLearned();
    } catch (error) {
      if (started !== generation || !userId) return;
      status(error.message || 'Could not load preferences.', true);
      el('preferences-retry').hidden = false;
    }
  }
  el('preferences-retry').onclick = load;
  el('preferences-form').onsubmit = async event => {
    event.preventDefault();
    if (!loaded || !userId || saving) return;
    const started = generation;
    saving = true;
    el('preferences-fields').disabled = true;
    status('Saving…');
    try {
      const response = await request('/v1/preferences', {method: 'PUT', body: JSON.stringify({
        display_density: el('display-density').value,
        shopping_priority: el('shopping-priority').value,
        proactive_enabled: el('proactive-enabled').checked,
        notes: el('user-notes').value,
      })});
      if (!response.ok) throw new Error('Could not save. Your changes are still here; try again.');
      const values = await response.json();
      if (started !== generation || !userId) return;
      populate(values);
      status('Saved to your account.');
      loadLearned();
    } catch (error) {
      if (started !== generation || !userId) return;
      status(error.message || 'Could not save. Please try again.', true);
    } finally {
      if (started === generation) {
        saving = false;
        el('preferences-fields').disabled = !loaded;
      }
    }
  };
  return {
    setUser(id) {
      generation++;
      userId = id;
      loaded = false;
      saving = false;
      populate({});
      showLearned(null);
      el('preferences-fields').disabled = true;
      el('preferences-retry').hidden = true;
      if (id) {
        const started = generation;
        // Auth callbacks run under the SDK lock; defer the authenticated read.
        setTimeout(() => {if (started === generation) load();}, 0);
      }
      else status('Sign in to load your preferences.');
    },
  };
}
