(() => {
  const chart = document.getElementById("sales-performance-chart");
  const dataElement = document.getElementById("sales-chart-data");
  if (!chart || !dataElement) return;

  const errorMessage = document.querySelector("[data-chart-error]");
  const svgNS = "http://www.w3.org/2000/svg";
  const create = (name, attributes = {}) => {
    const element = document.createElementNS(svgNS, name);
    Object.entries(attributes).forEach(([key, value]) => element.setAttribute(key, value));
    return element;
  };

  try {
    const points = JSON.parse(dataElement.textContent);
    if (!Array.isArray(points) || points.length !== 7) throw new Error("Expected seven daily values");

    const width = 720;
    const height = 250;
    const padding = { top: 20, right: 16, bottom: 36, left: 68 };
    const plotWidth = width - padding.left - padding.right;
    const plotHeight = height - padding.top - padding.bottom;
    const amounts = points.map((point) => Number(point.amount));
    if (amounts.some((amount) => !Number.isFinite(amount) || amount < 0)) throw new Error("Invalid chart value");
    const maxValue = Math.max(...amounts);
    const axisMax = maxValue > 0 ? maxValue * 1.15 : 1;
    const currency = new Intl.NumberFormat("en-GH", { style: "currency", currency: "GHS", maximumFractionDigits: 0 });
    const x = (index) => padding.left + (plotWidth * index) / (points.length - 1);
    const y = (amount) => padding.top + plotHeight - (amount / axisMax) * plotHeight;

    for (let tick = 0; tick <= 3; tick += 1) {
      const value = (axisMax * tick) / 3;
      const tickY = y(value);
      chart.appendChild(create("line", { x1: padding.left, y1: tickY, x2: width - padding.right, y2: tickY, class: "chart-grid-line" }));
      const label = create("text", { x: padding.left - 10, y: tickY + 4, class: "chart-axis-label", "text-anchor": "end" });
      label.textContent = currency.format(value);
      chart.appendChild(label);
    }

    const coordinates = points.map((point, index) => `${x(index)},${y(amounts[index])}`).join(" ");
    const fillPoints = `${x(0)},${y(0)} ${coordinates} ${x(points.length - 1)},${y(0)}`;
    chart.appendChild(create("polygon", { points: fillPoints, class: "chart-area" }));
    chart.appendChild(create("polyline", { points: coordinates, class: "chart-line" }));

    points.forEach((point, index) => {
      const circle = create("circle", { cx: x(index), cy: y(amounts[index]), r: 4, class: "chart-point" });
      const title = create("title");
      title.textContent = `${point.date}: ${currency.format(amounts[index])}`;
      circle.appendChild(title);
      chart.appendChild(circle);
      const label = create("text", { x: x(index), y: height - 11, class: "chart-axis-label", "text-anchor": "middle" });
      label.textContent = point.label;
      chart.appendChild(label);
    });
  } catch (error) {
    chart.hidden = true;
    if (errorMessage) errorMessage.hidden = false;
  }
})();
