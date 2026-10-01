(() => {
  document.querySelectorAll('.logout-form').forEach(form => {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button[type="submit"]');
      if (!button || button.disabled) return;
      button.disabled = true;
      button.textContent = 'Signing out...';
      form.setAttribute('aria-busy', 'true');
    });
  });

  const meter = document.querySelector('#api-bandwidth-meter');
  const details = document.querySelector('#bandwidth-details');
  if ((!meter && !details) || !('PerformanceObserver' in window)) return;

  const bytesLabel = meter?.querySelector('[data-bandwidth-bytes]');
  const countLabel = meter?.querySelector('[data-bandwidth-count]');
  const detailsBytesLabel = details?.querySelector('[data-bandwidth-bytes]');
  const detailsCountLabel = details?.querySelector('[data-bandwidth-count]');
  const monthLabel = document.querySelector('[data-bandwidth-month]');
  const monthTotalLabel = document.querySelector('[data-bandwidth-month-total]');
  const monthLog = document.querySelector('[data-bandwidth-month-log]');
  const today = () => {
    const date = new Date();
    return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`;
  };
  const storageKey = 'lora-api-bandwidth';
  const monthStorageKey = 'lora-api-bandwidth-month';
  let usage = { date: today(), bytes: 0, responses: 0 };
  let monthUsage = { month: today().slice(0, 7), days: {} };

  try {
    const saved = JSON.parse(localStorage.getItem(storageKey) || 'null');
    if (saved?.date === today()) usage = saved;
    const savedMonth = JSON.parse(localStorage.getItem(monthStorageKey) || 'null');
    if (savedMonth?.month === monthUsage.month && savedMonth.days) monthUsage = savedMonth;
    if (usage.bytes || usage.responses) {
      const savedDay = monthUsage.days[usage.date] || { bytes: 0, responses: 0 };
      monthUsage.days[usage.date] = {
        bytes: Math.max(savedDay.bytes, usage.bytes),
        responses: Math.max(savedDay.responses, usage.responses),
      };
    }
  } catch (error) {
    // Keep counting in memory when browser storage is unavailable.
  }

  const formatBytes = bytes => {
    if (bytes < 1024) return `${bytes} B`;
    const units = ['KB', 'MB', 'GB', 'TB'];
    let value = bytes / 1024;
    let unit = 0;
    while (value >= 1024 && unit < units.length - 1) {
      value /= 1024;
      unit += 1;
    }
    return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
  };

  const render = () => {
    const currentDate = today();
    const currentMonth = currentDate.slice(0, 7);
    if (usage.date !== currentDate) usage = { date: currentDate, bytes: 0, responses: 0 };
    if (monthUsage.month !== currentMonth) monthUsage = { month: currentMonth, days: {} };
    const bytes = formatBytes(usage.bytes);
    const responses = `${usage.responses} ${usage.responses === 1 ? 'response' : 'responses'}`;
    if (bytesLabel) bytesLabel.textContent = bytes;
    if (countLabel) countLabel.textContent = responses;
    if (detailsBytesLabel) detailsBytesLabel.textContent = bytes;
    if (detailsCountLabel) detailsCountLabel.textContent = responses;
    if (monthLabel) monthLabel.textContent = new Intl.DateTimeFormat(undefined, { month: 'long', year: 'numeric' }).format(new Date(`${currentMonth}-01T12:00:00`));
    if (monthTotalLabel) {
      monthTotalLabel.textContent = formatBytes(Object.values(monthUsage.days).reduce((total, day) => total + day.bytes, 0));
    }
    if (monthLog) {
      const days = Object.entries(monthUsage.days).sort(([left], [right]) => right.localeCompare(left));
      monthLog.replaceChildren();
      if (!days.length) {
        const row = document.createElement('tr');
        row.innerHTML = '<td colspan="3" class="empty-log">No daily totals recorded this month.</td>';
        monthLog.append(row);
      } else {
        days.forEach(([date, day]) => {
          const row = document.createElement('tr');
          [date, formatBytes(day.bytes), String(day.responses)].forEach(value => {
            const cell = document.createElement('td');
            cell.textContent = value;
            row.append(cell);
          });
          monthLog.append(row);
        });
      }
    }
  };

  const observer = new PerformanceObserver(entries => {
    render();
    for (const entry of entries.getEntries()) {
      if (!entry.name.startsWith(`${location.origin}/api/`)) continue;
      usage.bytes += entry.transferSize || 0;
      usage.responses += 1;
    }
    monthUsage.days[usage.date] = { bytes: usage.bytes, responses: usage.responses };
    try {
      localStorage.setItem(storageKey, JSON.stringify(usage));
      localStorage.setItem(monthStorageKey, JSON.stringify(monthUsage));
    } catch (error) {
      // The visible total remains available for this page session.
    }
    render();
  });

  observer.observe({ type: 'resource', buffered: true });
  window.addEventListener('storage', event => {
    if (!event.newValue) return;
    try {
      const saved = JSON.parse(event.newValue);
      if (event.key === storageKey && saved.date === today()) {
        usage = saved;
        render();
      } else if (event.key === monthStorageKey && saved.month === today().slice(0, 7)) {
        monthUsage = saved;
        render();
      }
    } catch (error) {
      return;
    }
  });
  window.setInterval(render, 60000);
  render();
})();