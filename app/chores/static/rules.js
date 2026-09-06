document.querySelectorAll('[data-rule-editor]').forEach(function (form) {
  function preview() {
    const weekday = Number(form.elements.weekday.value);
    const condition = form.elements.condition_type.value;
    const threshold = form.elements.points_threshold;
    const minutes = Number(form.elements.reward_minutes.value);
    const ends = form.elements.ends_cycle;
    threshold.readOnly = condition === 'none';
    ends.setCustomValidity(ends.checked && weekday !== 6 ? '结束周期规则只能设置在周日' : '');
    const requirement = condition === 'none' ? '无条件自动获得' :
      (condition === 'week_total' ? '本周累计' : '当天累计') + threshold.value + ' 分后自动解锁';
    form.querySelector('output').textContent = '周' + '一二三四五六日'[weekday] + ' · ' + requirement +
      ' ' + Math.floor(minutes / 60) + ' 小时 ' + minutes % 60 + ' 分 · 当天有效' +
      (ends.checked ? ' · 然后结束本周' : '');
  }
  form.addEventListener('input', preview);
  form.addEventListener('change', preview);
  form.addEventListener('submit', function (event) {
    var scope = form.elements.effective_scope;
    if (scope && scope.value === 'current_cycle' && !window.confirm('本周立即采用只会更新尚未执行的规则。已经发放的时长不会回收，是否继续？')) {
      event.preventDefault();
    }
  });
  preview();
});
