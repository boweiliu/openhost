// Loaded synchronously in <head> so the theme is applied before first paint.
// The choice lives in this browser's localStorage; data-theme on <html> carries
// it to the stylesheets. "sky" is the default look and "classic" is the flat,
// system-font look. A new theme is one more entry here plus one stylesheet
// scoped to :root[data-theme="<name>"].
(function () {
  const STORAGE_KEY = 'openhost-theme';
  const CHOICES = ['sky', 'classic'];
  const DEFAULT_CHOICE = 'sky';

  function getChoice() {
    const stored = window.localStorage.getItem(STORAGE_KEY);
    return CHOICES.includes(stored) ? stored : DEFAULT_CHOICE;
  }

  function apply() {
    document.documentElement.dataset.theme = getChoice();
  }

  function setChoice(choice) {
    if (!CHOICES.includes(choice)) throw new Error('Unknown theme: ' + choice);
    window.localStorage.setItem(STORAGE_KEY, choice);
    apply();
  }

  apply();

  window.OpenHostTheme = {choices: CHOICES, getChoice, setChoice};
})();
