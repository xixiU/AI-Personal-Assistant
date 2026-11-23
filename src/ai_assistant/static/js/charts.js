/**
 * 极简 SVG 折线/柱状图，无第三方依赖（断网可用）
 * Charts.line(el, items, series)  items: [{date, ...}], series: [{key, label, color}]
 */
(function (global) {
    'use strict';

    var NS = 'http://www.w3.org/2000/svg';
    function node(tag, attrs, text) {
        var el = document.createElementNS(NS, tag);
        Object.keys(attrs || {}).forEach(function (k) { el.setAttribute(k, attrs[k]); });
        if (text !== undefined) el.textContent = text;
        return el;
    }
    function niceMax(v) {
        if (v <= 5) return 5;
        var p = Math.pow(10, Math.floor(Math.log10(v)));
        var n = v / p;
        return (n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
    }

    var Charts = {
        line: function (host, items, series) {
            host.innerHTML = '';
            if (!items || !items.length) {
                host.innerHTML = '<div class="empty">暂无数据</div>';
                return;
            }
            var W = host.clientWidth || 600, H = 240;
            var pad = { l: 40, r: 12, t: 12, b: 28 };
            var iw = W - pad.l - pad.r, ih = H - pad.t - pad.b;
            var max = 0;
            items.forEach(function (it) { series.forEach(function (s) { max = Math.max(max, +it[s.key] || 0); }); });
            max = niceMax(max);

            var svg = node('svg', { 'class': 'chart', viewBox: '0 0 ' + W + ' ' + H, role: 'img' });
            for (var i = 0; i <= 4; i++) {
                var y = pad.t + ih - ih * i / 4;
                svg.appendChild(node('line', { x1: pad.l, x2: W - pad.r, y1: y, y2: y, stroke: 'var(--border)' }));
                svg.appendChild(node('text', { x: pad.l - 6, y: y + 4, 'text-anchor': 'end' }, String(Math.round(max * i / 4))));
            }
            var step = items.length > 1 ? iw / (items.length - 1) : 0;
            var labelEvery = Math.ceil(items.length / 8);
            items.forEach(function (it, idx) {
                if (idx % labelEvery === 0 || idx === items.length - 1) {
                    svg.appendChild(node('text', { x: pad.l + step * idx, y: H - 8, 'text-anchor': 'middle' },
                        String(it.date || '').slice(5)));
                }
            });
            series.forEach(function (s) {
                var pts = items.map(function (it, idx) {
                    return (pad.l + step * idx) + ',' + (pad.t + ih - ih * (+it[s.key] || 0) / max);
                }).join(' ');
                svg.appendChild(node('polyline', { points: pts, fill: 'none', stroke: s.color, 'stroke-width': 2, 'stroke-linejoin': 'round' }));
                items.forEach(function (it, idx) {
                    var c = node('circle', { cx: pad.l + step * idx, cy: pad.t + ih - ih * (+it[s.key] || 0) / max, r: 3, fill: s.color });
                    c.appendChild(node('title', {}, it.date + ' ' + s.label + '：' + (+it[s.key] || 0)));
                    svg.appendChild(c);
                });
            });
            host.appendChild(svg);

            var legend = document.createElement('div');
            legend.className = 'legend';
            series.forEach(function (s) {
                var span = document.createElement('span');
                span.innerHTML = '<i style="background:' + s.color + '"></i>';
                span.appendChild(document.createTextNode(s.label));
                legend.appendChild(span);
            });
            host.appendChild(legend);
        },

        /** 横向条形列表（高频问题） items: [{query, count}] */
        bars: function (host, items) {
            host.innerHTML = '';
            if (!items || !items.length) {
                host.innerHTML = '<div class="empty">暂无数据</div>';
                return;
            }
            var max = Math.max.apply(null, items.map(function (i) { return +i.count || 0; })) || 1;
            items.forEach(function (it) {
                var row = document.createElement('div');
                row.className = 'row';
                var q = document.createElement('div'); q.className = 'q'; q.textContent = it.query; q.title = it.query;
                var n = document.createElement('div'); n.className = 'n'; n.textContent = it.count;
                var track = document.createElement('div'); track.className = 'track';
                var fill = document.createElement('div'); fill.className = 'fill';
                fill.style.width = (100 * (+it.count || 0) / max) + '%';
                track.appendChild(fill);
                row.appendChild(q); row.appendChild(n); row.appendChild(track);
                host.appendChild(row);
            });
        }
    };

    global.Charts = Charts;
})(window);
