/* ============================================================================
 * 真实课件重绘 + 课件操作执行器
 * ----------------------------------------------------------------------------
 * 这个文件是"AI 讲师真正操作 PPT"的前端一半。
 *
 * 定位：后端把 pptx 解析成"元素 + 段落 + 样式"的纯数据（deck/schema.py），
 * 这里只负责**照着画**和**照做**，不做任何解析、不猜任何缺失字段。
 *
 * 两条渲染路径：
 *   deck   —— 真实 PPT 重绘。参考画布固定 1280×720（EMU 按 96dpi 换算），
 *             整块 `transform: scale()` 自适应容器宽度。这样**文字换行是确定性的**
 *             （不随窗口大小重排），字号 px = pt×4/3，与 PowerPoint 96dpi 一致。
 *   simple —— 原型自带的堆叠版式（手写演示课件），保持旧外观不变。
 *
 * 可操作对象有两种粒度，都注册进 `els`：
 *   element_id（形状）
 *   para_id（段落）  → 重点提示是同一个"脉冲光圈"，只是落点粒度不同
 * 段落级粒度是接真实 PPT 的关键：单词课件把 音标/释义/搭配/例句 全塞在一个文本框里，
 * 只有细到段落，"我们看例句这一行"才真的指得到。
 * ==========================================================================*/
(function (global) {
  'use strict';

  // 形状预设：位置与尺寸来自 pptx（精确），轮廓用 CSS 近似（够上课用）。
  // 解析器对认不出的预设会退化成 rect + 保留 preset 字段 —— 前端在这里把它补回来，
  // 否则"目录页的 01/02/03/04"会从箭头变成方块（实测第 2 页就是这个差别）。
  const PRESET_CLIP = {
    chevron: 'polygon(0 0, 86% 0, 100% 50%, 86% 100%, 0 100%, 14% 50%)',
    homePlate: 'polygon(0 0, 78% 0, 100% 50%, 78% 100%, 0 100%)',
    rightArrow: 'polygon(0 25%, 64% 25%, 64% 0, 100% 50%, 64% 100%, 64% 75%, 0 75%)',
    leftArrow: 'polygon(36% 25%, 100% 25%, 100% 75%, 36% 75%, 36% 100%, 0 50%, 36% 0)',
    triangle: 'polygon(50% 0, 100% 100%, 0 100%)',
    isocelesTriangle: 'polygon(50% 0, 100% 100%, 0 100%)',
    rtTriangle: 'polygon(0 0, 100% 100%, 0 100%)',
    diamond: 'polygon(50% 0, 100% 50%, 50% 100%, 0 50%)',
    parallelogram: 'polygon(22% 0, 100% 0, 78% 100%, 0 100%)',
    trapezoid: 'polygon(22% 0, 78% 0, 100% 100%, 0 100%)',
    pentagon: 'polygon(50% 0, 100% 38%, 81% 100%, 19% 100%, 0 38%)',
    hexagon: 'polygon(25% 0, 75% 0, 100% 50%, 75% 100%, 25% 100%, 0 50%)',
    star5: 'polygon(50% 0, 61% 35%, 98% 35%, 68% 57%, 79% 91%, 50% 70%, 21% 91%,'
      + ' 32% 57%, 2% 35%, 39% 35%)',
    plus: 'polygon(35% 0, 65% 0, 65% 35%, 100% 35%, 100% 65%, 65% 65%, 65% 100%,'
      + ' 35% 100%, 35% 65%, 0 65%, 0 35%, 35% 35%)',
    donut: 'none',          // 用 border-radius 表达更准，交给 SHAPE_CLIP
    ellipse: 'none',
  };
  const SHAPE_CLIP = {
    ellipse: (w, h) => `border-radius:${w / 2}px/${h / 2}px`,
    roundRect: (w, h) => `border-radius:${Math.min(18, Math.min(w, h) * 0.14)}px`,
  };

  let opts = {};                 // {onLog, onOps, onPageInfo}
  let box = {};                  // {stagewrap, stage, zoomlayer, slide, pointer, pageflip, simplehost}
  let els = {};                  // anchor_id -> DOM 节点
  let host = null;               // 当前渲染宿主
  let deckMode = true;
  let size = {w: 1280, h: 720};
  let zoom = {on: false, scale: 1, cx: 0, cy: 0};
  let fsMode = false;            // 全屏：按宽高双约束缩放，并在容器里居中
  let mediaBase = '';
  let stats = {sent: 0, hit: 0, miss: 0, last: null};

  /* ------------------------------------------------------------------ 小工具 */
  const $ = id => document.getElementById(id);

  function hexAlpha(a) {
    return Math.max(0, Math.min(255, Math.round(a * 255))).toString(16).padStart(2, '0');
  }
  function cssColor(c, a) {
    return (c || '#000') + (a != null && a < 1 ? hexAlpha(a) : '');
  }
  function fillCss(f) {
    if (!f || f.kind === 'none') return 'transparent';
    if (f.kind === 'solid') return cssColor(f.color, f.alpha);
    if (f.kind === 'gradient' && f.gradient && f.gradient.length) {
      const stops = f.gradient
        .map(s => `${cssColor(s.color, s.alpha)} ${Math.round((s.pos || 0) * 100)}%`)
        .join(',');
      // 角度已由解析器从 DrawingML（1/60000 度、0=从左到右）换算成 CSS 约定（0=向上）
      return `linear-gradient(${(f.angle || 0).toFixed(2)}deg, ${stops})`;
    }
    if (f.kind === 'image') return '#eef1f6';
    return 'transparent';
  }
  function lineCss(l) {
    // 渐变/图片描边：CSS 的 border 表达不了（clip-path 之后更会走形）。
    // 这种情况**不要描成黑色** —— 之前就是这里把母版上一条渐变描边画成了两道黑带，
    // 页面对比反而更差。宁可不画（少一条细线），也不要画错。
    if (!l || l.kind === 'none' || !l.width_px) return 'none';
    if (l.kind !== 'solid') {
      if (opts.onLog) opts.onLog(`渐变描边未绘制（${l.kind}）`, 'lwarn');
      return 'none';
    }
    const style = l.dash ? (l.dash.indexOf('dot') >= 0 ? 'dotted' : (l.dash === 'dash' ? 'dashed' : 'solid')) : 'solid';
    return `${l.width_px.toFixed(1)}px ${style} ${cssColor(l.color, l.alpha)}`;
  }
  function fontStack(run) {
    const fams = [];
    if (run.font) fams.push(`"${run.font}"`);
    if (run.font_ea) fams.push(`"${run.font_ea}"`);
    fams.push('"Microsoft YaHei"', '"MiSans"', '"PingFang SC"', 'system-ui', 'sans-serif');
    return fams.join(',');
  }
  function runStyle(run, scale) {
    const pt2px = v => (v * scale * 4 / 3).toFixed(2);
    let s = `font-family:${fontStack(run)};`;
    if (run.size_pt != null) s += `font-size:${pt2px(run.size_pt)}px;`;
    if (run.color) s += `color:${run.color};`;
    if (run.bold) s += 'font-weight:700;';
    if (run.italic) s += 'font-style:italic;';
    const deco = [];
    if (run.underline) deco.push('underline');
    if (run.strike) deco.push('line-through');
    if (deco.length) s += `text-decoration:${deco.join(' ')};`;
    if (run.baseline === 'superscript') s += 'vertical-align:super;font-size:.75em;';
    if (run.baseline === 'subscript') s += 'vertical-align:sub;font-size:.75em;';
    return s;
  }
  function paraStyle(p, scale) {
    let s = '';
    if (p.align) s += `text-align:${p.align};`;
    if (p.line_spacing) s += `line-height:${p.line_spacing};`;
    if (p.space_before_pt) s += `margin-top:${(p.space_before_pt * scale * 4 / 3).toFixed(2)}px;`;
    if (p.space_after_pt) s += `margin-bottom:${(p.space_after_pt * scale * 4 / 3).toFixed(2)}px;`;
    // 缩进：左边距 + 首行偏移。首行偏移是负数时就是悬挂缩进 ——
    // 项目符号跟着首行一起左移，正好落进悬挂区（PowerPoint 的排版方式）。
    const ml = p.margin_left_px || 0, fl = p.first_line_px || 0;
    if (ml || fl) {
      s += `padding-left:${(ml * scale).toFixed(2)}px;`;
      s += `text-indent:${(fl * scale).toFixed(2)}px;`;
    } else if (p.indent_px) {
      s += `text-indent:${(p.indent_px * scale).toFixed(2)}px;`;
    }
    return s;
  }

  /* --------------------------------------------------------------- 元素渲染 */
  function buildTextLayer(el, scale) {
    const t = el.text;
    const box2 = document.createElement('div');
    box2.className = 'txt';
    const ins = (t.insets_px && t.insets_px.length === 4) ? t.insets_px : [9.6, 4.8, 9.6, 4.8];
    box2.style.padding =
      `${ins[1] * scale}px ${ins[2] * scale}px ${ins[3] * scale}px ${ins[0] * scale}px`;
    box2.style.justifyContent = t.anchor === 'middle' ? 'center'
      : (t.anchor === 'bottom' ? 'flex-end' : 'flex-start');
    const fs = (t.font_scale && t.font_scale !== 1) ? t.font_scale : 1;
    const sc = scale * fs;
    for (const p of (t.paras || [])) {
      const d = document.createElement('div');
      d.className = 'para' + (p.level ? ` lv${Math.min(4, p.level)}` : '');
      d.dataset.anchorId = p.pid || '';
      d.style.cssText = paraStyle(p, scale);
      if (p.bullet === 'char' || p.bullet === 'auto') {
        d.classList.add('bullet-char');
        // buChar 没写字符时用通用圆点（母版上常见只给 buFont 不给 buChar 的情况）
        d.dataset.bullet = p.bullet_char || '•';
      }
      // 段落级默认样式打在容器上，run 级样式覆盖它 —— 真实课件里
      // 字号/颜色常常写在段落 defRPr 或版式上，run 上什么都没有。
      let ds = '';
      if (p.default_size_pt != null) ds += `font-size:${(p.default_size_pt * sc * 4 / 3).toFixed(2)}px;`;
      if (p.default_color) ds += `color:${p.default_color};`;
      if (p.default_font) ds += `font-family:${fontStack({font: p.default_font})};`;
      if (p.default_bold) ds += 'font-weight:700;';
      if (ds) d.style.cssText += ds;
      if (!p.runs || !p.runs.length) {
        d.textContent = p.text || '';
      } else {
        for (const r of p.runs) {
          const sp = document.createElement('span');
          sp.style.cssText = runStyle(r, sc);
          sp.textContent = r.text || '';
          d.appendChild(sp);
        }
      }
      box2.appendChild(d);
    }
    return box2;
  }

  function pictureNode(el) {
    const wrap = document.createElement('div');
    wrap.className = 'shape';
    wrap.style.background = '#eef1f6';
    const p = el.pic || {};
    const extUrl = p.external_url || '';
    const httpExt = /^https?:/i.test(extUrl) ? extUrl : '';
    if (p.src || httpExt) {
      const img = document.createElement('img');
      img.src = p.src ? mediaBase + String(p.src).replace(/^media\//, '') : httpExt;
      img.alt = p.alt || el.name || '';
      img.loading = 'lazy';
      // 图片取不到时必须**看得见地**失败。
      // deck.json 里的图片路径是相对 deck 目录的（media/xxx.jpeg），
      // 一旦 deck.json 与 media/ 被分开放，默认行为是"33 张图全部静默变白"——
      // 上课时只会觉得"这课件怎么空着"，反而更难查。
      img.onerror = () => {
        // 兜底一：后端没下下来的外链图，让浏览器自己去取
        if (httpExt && img.src !== httpExt) {
          img.src = httpExt;
          return;
        }
        wrap.style.background = 'repeating-linear-gradient(45deg,#f4f6fa,#f4f6fa 8px,#e7ebf3 8px,#e7ebf3 16px)';
        const d = document.createElement('div');
        d.className = 'unrender';
        d.style.cssText = 'position:absolute;inset:auto 0 50% 0;text-align:center;font-size:12px;color:#b33;opacity:1';
        d.textContent = (extUrl ? '外链图片未取到：' : '图片缺失：') + (p.src || extUrl).slice(0, 80);
        wrap.appendChild(d);
        if (opts.onLog) opts.onLog(`图片没取到：${p.src || extUrl}（检查 decks/<deck_id>/media/ 是否还在）`, 'lerror');
      };
      if (p.crop && p.crop.some(v => v)) {
        // srcRect 裁剪：用 clip-path + 放大 + 负边距表达
        const l = p.crop[0], t = p.crop[1], r = p.crop[2], b = p.crop[3];
        const kw = 1 - l - r, kh = 1 - t - b;
        img.style.clipPath = `inset(${t * 100}% ${r * 100}% ${b * 100}% ${l * 100}%)`;
        img.style.width = `${100 / kw}%`;
        img.style.height = `${100 / kh}%`;
        img.style.marginLeft = `${-l / kw * 100}%`;
        img.style.marginTop = `${-t / kh * 100}%`;
        img.style.position = 'absolute';
      }
      wrap.appendChild(img);
    } else {
      const d = document.createElement('div');
      d.className = 'unrender';
      d.textContent = '图片未取到' + (p.external_url ? '（外链）' : '');
      wrap.appendChild(d);
    }
    if (el.line && el.line.kind !== 'none') wrap.style.border = lineCss(el.line);
    return wrap;
  }

  function tableNode(el, scale) {
    const wrap = document.createElement('div');
    wrap.className = 'shape';
    const t = document.createElement('table');
    const data = el.table || {};
    const cg = document.createElement('colgroup');
    (data.col_w_px || []).forEach(w => {
      const c = document.createElement('col');
      c.style.width = `${w * scale}px`;
      cg.appendChild(c);
    });
    t.appendChild(cg);
    (data.cells || []).forEach((row, ri) => {
      const tr = document.createElement('tr');
      if (data.row_h_px && data.row_h_px[ri]) tr.style.height = `${data.row_h_px[ri] * scale}px`;
      row.forEach(cell => {
        const td = document.createElement('td');
        if (cell.col_span > 1) td.colSpan = cell.col_span;
        if (cell.row_span > 1) td.rowSpan = cell.row_span;
        if (cell.fill && cell.fill.kind !== 'none') td.style.background = fillCss(cell.fill);
        td.textContent = cell.text || '';
        td.dataset.anchorId = cell.pid || '';
        tr.appendChild(td);
      });
      t.appendChild(tr);
    });
    wrap.appendChild(t);
    return wrap;
  }

  let gradSeq = 0;      // SVG 渐变 id 计数器（每个渐变必须是独立 id）

  /** 给 SVG 用的渐变定义。
   *
   * 关键坑：**SVG 的 fill 属性不认 CSS 的 `linear-gradient(...)`**，
   * 传进去是"非法值"，浏览器会当成没写 —— 结果是**黑色实心**。
   * 之前母版上那块浅色箭头就这么被画成了两道黑带（页面对比反而更差）。
   * 所以要在 SVG 里老老实实建 <linearGradient>。
   */
  function svgGradient(svg, f) {
    const NS = 'http://www.w3.org/2000/svg';
    const defs = document.createElementNS(NS, 'defs');
    const gid = 'deggrad' + (++gradSeq);
    const lg = document.createElementNS(NS, 'linearGradient');
    lg.setAttribute('id', gid);
    const ang = (f.angle || 0) * Math.PI / 180;      // CSS 约定：0° = 向上
    const dx = Math.sin(ang), dy = -Math.cos(ang);
    lg.setAttribute('x1', (0.5 - dx / 2).toFixed(3));
    lg.setAttribute('y1', (0.5 - dy / 2).toFixed(3));
    lg.setAttribute('x2', (0.5 + dx / 2).toFixed(3));
    lg.setAttribute('y2', (0.5 + dy / 2).toFixed(3));
    for (const s of f.gradient) {
      const stop = document.createElementNS(NS, 'stop');
      stop.setAttribute('offset', Math.round((s.pos || 0) * 100) + '%');
      stop.setAttribute('stop-color', cssColor(s.color, s.alpha));
      lg.appendChild(stop);
    }
    defs.appendChild(lg);
    svg.appendChild(defs);
    return `url(#${gid})`;
  }

  function svgNode(el, b) {
    const wrap = document.createElement('div');
    wrap.className = 'svgshape';
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${Math.max(1, b[2])} ${Math.max(1, b[3])}`);
    svg.setAttribute('preserveAspectRatio', 'none');
    const f = el.fill, l = el.line;
    const d = el.svg_path || '';
    if (d) {
      const path = document.createElementNS(NS, 'path');
      path.setAttribute('d', d);
      // 填充：纯色直接给，渐变必须走 <linearGradient>（见 svgGradient 的说明）
      if (f && f.kind === 'gradient' && f.gradient && f.gradient.length) {
        path.setAttribute('fill', svgGradient(svg, f));
      } else if (f && f.kind !== 'none' && f.kind !== 'image') {
        path.setAttribute('fill', fillCss(f));
      } else {
        path.setAttribute('fill', 'none');
      }
      if (l && l.kind !== 'none' && l.width_px) {
        path.setAttribute('stroke', cssColor(l.color, l.alpha));
        path.setAttribute('stroke-width', String(l.width_px));
        path.setAttribute('stroke-linejoin', 'round');
        path.setAttribute('stroke-linecap', l.cap === 'round' ? 'round' : 'butt');
        if (l.dash) path.setAttribute('stroke-dasharray', l.dash.indexOf('dot') >= 0 ? '2 3' : '6 4');
      } else {
        path.setAttribute('stroke', 'none');
      }
      svg.appendChild(path);
    } else {
      // 没解析出 path 的线条：至少让学生看到"这里有一条线"
      const ln = document.createElementNS(NS, 'line');
      ln.setAttribute('x1', '0'); ln.setAttribute('y1', '0');
      ln.setAttribute('x2', String(b[2])); ln.setAttribute('y2', String(b[3]));
      ln.setAttribute('stroke',
        (l && l.kind !== 'none') ? cssColor(l.color, l.alpha) : (f ? fillCss(f) : '#888'));
      ln.setAttribute('stroke-width', String((l && l.width_px) || 1));
      svg.appendChild(ln);
    }
    wrap.appendChild(svg);
    return wrap;
  }

  function renderElement(el, scale, o) {
    const register = !o || o.register !== false;
    const b = el.box || [0, 0, 0, 0];
    const node = document.createElement('div');
    node.className = 'elbox' + (el.kind === 'unknown' ? ' unknown' : '');
    node.dataset.elementId = el.element_id;
    node.dataset.kind = el.kind;
    node.dataset.role = el.role || '';
    if (el.source && el.source !== 'slide') {
      node.dataset.source = el.source;
      node.style.pointerEvents = 'none';        // 装饰不可点/不可锚
    }
    node.style.left = (b[0] * scale).toFixed(2) + 'px';
    node.style.top = (b[1] * scale).toFixed(2) + 'px';
    node.style.width = (b[2] * scale).toFixed(2) + 'px';
    node.style.height = (b[3] * scale).toFixed(2) + 'px';
    // 层序：母版 < 版式 < 本页元素。三层的 z 是各自从 1 开始数的，
    // 不能直接相加比较（母版的 sh15 会跑到版式的 sh01 上面去）。
    const base = el.source === 'master' ? 0 : (el.source === 'layout' ? 1000 : 100000);
    node.style.zIndex = String(base + (el.z || 0));
    if (el.rot) node.style.transform = `rotate(${el.rot}deg)`;
    if (el.hidden && opts.honorReveal) node.classList.add('prehide');

    if (el.kind === 'picture') {
      node.appendChild(pictureNode(el));
    } else if (el.kind === 'table') {
      node.appendChild(tableNode(el, scale));
    } else if (el.kind === 'freeform' || el.kind === 'line' || el.kind === 'connector') {
      node.appendChild(svgNode(el, b));
    } else {
      const shape = document.createElement('div');
      shape.className = 'shape';
      shape.style.background = fillCss(el.fill);
      const clip = SHAPE_CLIP[el.kind];
      if (clip) shape.style.cssText += ';' + clip(b[2] * scale, b[3] * scale);
      // 形状预设：解析器认不出轮廓时会保留 preset 并退化成矩形，这里按预设裁出来
      const byPreset = el.preset && el.preset !== 'rect' ? PRESET_CLIP[el.preset] : '';
      if (byPreset && byPreset !== 'none') shape.style.clipPath = byPreset;
      if (el.line && el.line.kind !== 'none') shape.style.border = lineCss(el.line);
      node.appendChild(shape);
      if (el.kind === 'unknown') {
        const tag = document.createElement('div');
        tag.className = 'unrender';
        tag.textContent = `未渲染：${el.name || el.preset || el.kind}`;
        node.appendChild(tag);
      }
    }

    const hasText = el.text && (el.text.paras || []).some(
      p => (p.text || '').length || (p.runs || []).length);
    if (hasText) node.appendChild(buildTextLayer(el, scale));

    if (register) {
      els[el.element_id] = node;
      for (const p of ((el.text && el.text.paras) || [])) {
        if (!p.pid || !(p.text || '').length) continue;
        const pn = node.querySelector(`[data-anchor-id="${p.pid}"]`);
        if (pn) els[p.pid] = pn;
      }
    }
    return node;
  }

  /* ------------------------------------------------------------------ 渲染 */
  function renderDeck(slide) {
    deckMode = true;
    host = box.slide;
    host.innerHTML = '';
    els = {};
    resetOps(true);
    box.stage.style.display = '';
    box.simplehost.style.display = 'none';
    size = {w: slide.width || 1280, h: slide.height || 720};
    mediaBase = slide.media_base || '';
    box.stage.style.width = size.w + 'px';
    box.stage.style.height = size.h + 'px';
    host.style.width = size.w + 'px';
    host.style.height = size.h + 'px';
    host.style.background = slide.background ? fillCss(slide.background) : '#fff';
    // 先画版式/母版的装饰（来自 layout/master 的图形，只画不锚、不参与操作），
    // 再画本页元素 —— 顺序就是层序，装饰必须在最下面。
    for (const el of (slide.decorations || [])) {
      host.appendChild(renderElement(el, 1, {register: false}));
    }
    for (const el of (slide.elements || [])) host.appendChild(renderElement(el, 1));
    // 课堂演示模式直接展示完整真实的 PPT，不再预隐藏元素；仅在预览页勾选分步模式时启用隐藏
    if (opts.honorReveal) {
      for (const id of (slide.reveal || [])) {
        const n = els[id];
        if (n) n.classList.add('prehide');
      }
    }
    fit();
    if (opts.onPageInfo) opts.onPageInfo({
      mode: 'deck', index: slide.index, title: slide.title,
      elements: (slide.elements || []).length, steps: (slide.steps || []).length,
      size: `${size.w}×${size.h}`,
    });
  }

  function renderSimple(slide) {
    deckMode = false;
    host = box.simplehost;
    host.style.display = 'block';
    box.stage.style.display = 'none';
    host.innerHTML = '';
    els = {};
    resetOps(true);
    size = {w: 0, h: 0};
    box.stagewrap.style.height = '';
    box.stage.style.transform = '';
    box.pointer.classList.remove('on');
    for (const e of slide.elements) {
      if (e.kind === 'image') {          // 示意图：沿用原型内置的 SVG 模板
        const tpl = document.getElementById('svg-img');
        if (tpl) {
          const w = document.createElement('div');
          w.dataset.elementId = e.element_id;
          w.appendChild(tpl.content.cloneNode(true));
          els[e.element_id] = w;
          host.appendChild(w);
          continue;
        }
      }
      const d = document.createElement('div');
      d.className = 'el' + (e.reveal ? ' hidden' : '');
      d.dataset.kind = e.kind;
      d.dataset.elementId = e.element_id;
      if (e.reveal) d.dataset.reveal = '1';
      d.textContent = e.text;
      els[e.element_id] = d;
      host.appendChild(d);
    }
    if (opts.onPageInfo) opts.onPageInfo({mode: 'simple', index: slide.index, title: slide.title,
      elements: slide.elements.length, steps: 0, size: '堆叠版式'});
  }

  function render(slide) {
    if (!slide) return;
    if (slide.mode === 'deck' || slide.width) renderDeck(slide);
    else renderSimple(slide);
  }

  /* 自适应：容器 → 参考画布的 scale。
     页面里只按**宽度**贴合（纵向排布，高度跟着算）；全屏时按宽高**双约束**取小值
     再居中 —— 竖屏或超宽屏时留黑边，而不是把课件裁掉一块。 */
  function fit() {
    if (!deckMode || !size.w || !box.stagewrap.clientWidth) return;
    const wrap = box.stagewrap;
    const w = wrap.clientWidth;
    let k = Math.max(0.05, w / size.w);
    let dx = 0, dy = 0;
    if (fsMode && wrap.clientHeight) {
      k = Math.max(0.05, Math.min(w / size.w, wrap.clientHeight / size.h));
      dx = (w - size.w * k) / 2;
      dy = (wrap.clientHeight - size.h * k) / 2;
      wrap.style.height = '';              // 全屏高度交给 CSS（inset:0）
    } else {
      wrap.style.height = (size.h * k) + 'px';
    }
    // translate 必须写在 scale 前面：两者都作用在同一个 transform 上，而
    // translate 走的是父容器坐标系 —— 写反了居中偏移会被 scale 放大一倍。
    box.stage.style.transform = `translate(${dx}px,${dy}px) scale(${k})`;
    // 标记画在 stagewrap 的像素坐标里、不跟 stage 一起 scale，所以缩放一变
    // 就得重新算一次落点（全屏、转屏、窗口拉伸都走这里）。
    if (mkNode && (box.marker || box.pointer)) placeMarker(mkNode);
  }

  /* -------------------------------------------------------------- 操作执行 */
  function nodeFor(anchorId) {
    if (!anchorId) return null;
    if (els[anchorId]) return els[anchorId];
    return host ? host.querySelector(`[data-anchor-id="${CSS.escape(anchorId)}"]`) : null;
  }
  function hostRect() {
    return (deckMode ? box.stage : box.simplehost).getBoundingClientRect();
  }
  function hostScale() {
    const r = hostRect();
    return deckMode ? size.w / Math.max(1, r.width) : 1;
  }
  function applyZoom() {
    if (!zoom.on || !deckMode) { box.zoomlayer.style.transform = ''; return; }
    // 目标点要落到可视区中心：transform 的实际位移 = scale × translate 的值，
    // 所以 translate 要除以 scale（这里推过一次，别凭直觉写）。
    box.zoomlayer.style.transformOrigin = `${zoom.cx}px ${zoom.cy}px`;
    const dx = (size.w / 2 - zoom.cx);
    const dy = (size.h / 2 - zoom.cy);
    box.zoomlayer.style.transform =
      `scale(${zoom.scale}) translate(${dx / zoom.scale}px, ${dy / zoom.scale}px)`;
  }

  /* 重点提示光标：取消整屏压暗蒙版，仅通过平滑移动的光标指示当前讲解重点。 */
  let mkNode = null;

  function cursorEl() {
    return box.marker || box.pointer || null;
  }

  function visibleTextRect(node) {
    if (!node || !document.createRange || !document.createTreeWalker) return null;
    const walker = document.createTreeWalker(node, NodeFilter.SHOW_TEXT, null);
    let firstNode = null, firstOff = 0, lastNode = null, lastOff = 0;
    let cur;
    while ((cur = walker.nextNode())) {
      const val = cur.nodeValue || '';
      const mStart = val.search(/\S/);
      if (mStart >= 0) {
        if (!firstNode) { firstNode = cur; firstOff = mStart; }
        const trimmed = val.replace(/\s+$/, '');
        lastNode = cur;
        lastOff = trimmed.length;
      }
    }
    if (!firstNode || !lastNode) return null;
    try {
      const r = document.createRange();
      r.setStart(firstNode, firstOff);
      r.setEnd(lastNode, lastOff);
      const rect = r.getBoundingClientRect();
      return (rect && rect.width > 1 && rect.height > 1) ? rect : null;
    } catch (e) {
      return null;
    }
  }

  function placeMarker(node) {
    const el = cursorEl();
    if (!el || !node || !box.stagewrap) return;
    const wr = box.stagewrap.getBoundingClientRect();
    const tr = visibleTextRect(node);
    let x, y;
    if (tr) {
      // 指向实际文字行左侧起点，既精准对齐缩进后的真实文字，又不遮挡正文内容
      x = Math.max(14, tr.left - wr.left - 8);
      y = tr.top - wr.top + Math.min(tr.height * 0.52, 22);
    } else {
      const nr = node.getBoundingClientRect();
      x = nr.left - wr.left + nr.width / 2;
      y = nr.top - wr.top + nr.height / 2;
    }
    el.style.left = x.toFixed(1) + 'px';
    el.style.top = y.toFixed(1) + 'px';
  }

  function pointAt(node) {
    if (!node) return;
    const id = node.dataset ? (node.dataset.anchorId || node.dataset.elementId) : '';
    if (id) { marker({element_id: id}); return; }
    mkNode = node;
    const el = cursorEl();
    if (!el) return;
    const wasOn = el.classList.contains('on');
    if (!wasOn) el.classList.add('snap');
    placeMarker(node);
    if (!wasOn) { void el.offsetWidth; el.classList.remove('snap'); }
    el.classList.add('on');
  }

  function marker(m) {
    const node = (m && m.element_id) ? nodeFor(m.element_id) : null;
    if (mkNode && mkNode !== node && mkNode.classList) mkNode.classList.remove('mk-target');
    mkNode = node;
    const el = cursorEl();
    if (box.pointer && box.marker && box.pointer !== el) box.pointer.classList.remove('on');
    if (!el) return;
    if (!node) { el.classList.remove('on'); return; }
    node.classList.add('mk-target');
    const wasOn = el.classList.contains('on');
    if (!wasOn) el.classList.add('snap');
    placeMarker(node);
    if (!wasOn) { void el.offsetWidth; el.classList.remove('snap'); }
    el.classList.add('on');
  }

  function resetOps(clearZoom) {
    marker(null);                       // 清场等于"不再提示任何地方"
    box.stage.classList.remove('focus');
    for (const k in els) {
      const n = els[k];
      if (n && n.classList) n.classList.remove('focus');
    }
    if (clearZoom && zoom.on) { zoom = {on: false, scale: 1, cx: 0, cy: 0}; applyZoom(); }
    if (box.pointer) box.pointer.classList.remove('on');
  }

  function applyOps(ops) {
    if (!ops || !ops.length) return;
    for (const op of ops) {
      stats.sent++;
      const ids = (op.targets && op.targets.length) ? op.targets
        : (op.target ? [op.target] : []);
      const nodes = ids.map(nodeFor).filter(Boolean);
      if (ids.length && !nodes.length) {
        stats.miss++;
        if (opts.onLog) opts.onLog(`课件操作未命中：${op.op} → ${ids.join(',')}`, 'lwarn');
      } else if (nodes.length) {
        stats.hit++;
      }
      switch (op.op) {
        case 'reveal':
        case 'step':
          nodes.forEach(n => n.classList.remove('prehide', 'hidden'));
          // 不再添加全屏压暗蒙版（stage.focus），只移动光标指示重点
          if (op.op === 'step' && ids.length) {
            marker({element_id: ids[0]});
          }
          break;
        case 'hide':
          if (opts.honorReveal) nodes.forEach(n => n.classList.add('prehide'));
          break;
        case 'highlight':
        case 'focus':
          marker(ids.length ? {element_id: ids[0]} : null);
          break;
        case 'unfocus':
          box.stage.classList.remove('focus');
          for (const k in els) if (els[k].classList) els[k].classList.remove('focus');
          marker(null);
          break;
        case 'point':
          if (ids.length) marker({element_id: ids[0]});
          else if (nodes[0]) pointAt(nodes[0]);
          break;
        case 'zoom':
          if (nodes[0]) {
            const b = nodes[0].getBoundingClientRect(), s = hostRect(), k = hostScale();
            zoom = {
              on: true, scale: op.scale || 1.7,
              cx: (b.left - s.left + b.width / 2) * k,
              cy: (b.top - s.top + b.height / 2) * k,
            };
            applyZoom();
          }
          break;
        case 'reset_zoom':
          zoom = {on: false, scale: 1, cx: 0, cy: 0}; applyZoom(); break;
        case 'clear':
          resetOps(false); break;
      }
    }
    stats.last = ops;
    if (opts.onOps) opts.onOps(stats);
  }

  /* 锚点提示（旧协议：随 segment_start 下发的 anchors）。
   现在它不再产生任何视觉高亮 —— 重点提示统一由 marker 的单个脉冲光圈承担，
   两套叠加会互相打架（黄框 + 光圈，学生分不清该看哪个）。
   留下的是它原本"顺手把该出现的元素放出来"的副作用：句子里点到的元素
   不能还藏在 prehide 里。标记本身不归它管。 */
  function highlight(anchors) {
    for (const a of (anchors || [])) {
      const n = nodeFor(a.element_id);
      if (!n || !n.classList) continue;
      if (n.dataset.reveal === '1') n.classList.remove('hidden');
      if (n.classList.contains('prehide')) n.classList.remove('prehide');
    }
  }

  function init(refs, options) {
    box = refs;
    opts = options || {};
    stats = {sent: 0, hit: 0, miss: 0, last: null};
    global.addEventListener('resize', fit);
  }

  /* 进/出全屏只做一件事：换缩放口径。高亮、指示器、课件操作全沿用原有换算。 */
  function setFullscreen(on) {
    fsMode = !!on;
    fit();
  }

  function debugElements() { return els; }
  function currentZoom() { return zoom; }

  global.DeckView = {
    init, render, fit, highlight, marker, applyOps, nodeFor, resetOps, setFullscreen,
    debugElements, currentZoom,
    get stats() { return stats; },
    get deckMode() { return deckMode; },
    get size() { return size; },
  };
})(window);
