"use strict";

const referenceKey = "ethercat-viewer-measurement-reference";
const defaults = {
  zero1: -0.9,
  zero4: -0.5,
  gain1: 1,
  gain4: 1,
  offset1: 0,
  offset4: 0,
};
const referenceForm = document.getElementById("reference-form");
const referenceSaved = document.getElementById("reference-saved");

function loadReference() {
  let values = defaults;
  try {
    values = {...defaults, ...JSON.parse(localStorage.getItem(referenceKey) || "{}")};
  } catch {
    referenceSaved.textContent = "使用默认参考值";
  }
  for (const [key, value] of Object.entries(defaults)) {
    referenceForm.elements[key].value = Number.isFinite(values[key]) ? values[key] : value;
  }
}

referenceForm.addEventListener("submit", event => {
  event.preventDefault();
  const values = {};
  for (const key of Object.keys(defaults)) {
    const value = referenceForm.elements[key].valueAsNumber;
    if (!Number.isFinite(value)) return;
    values[key] = value;
  }
  try {
    localStorage.setItem(referenceKey, JSON.stringify(values));
    referenceSaved.textContent = "已保存在当前浏览器";
  } catch {
    referenceSaved.textContent = "浏览器无法保存参考值";
  }
});
loadReference();
