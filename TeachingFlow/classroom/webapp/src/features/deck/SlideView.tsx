/**
 * 课件重绘（React 版）。
 * 参考画布固定 1280×720（EMU 按 96dpi 换算），整块 transform: scale() 自适应容器，
 * 这样文字换行是确定性的：字号 px = pt × 4/3，与 PowerPoint 96dpi 一致。
 */
import { useId, useLayoutEffect, useRef, useState, type CSSProperties } from 'react';
import type { DeckCell, DeckElement, DeckFill, DeckSlide } from './types';
import {
  clipFor,
  cssColor,
  fillCss,
  fontStack,
  lineCss,
  paraBaseStyle,
  paraStyle,
  resolveImageSrc,
  roundedStyle,
  runStyle,
} from './style';

interface ViewProps {
  slide: DeckSlide | null;
}

function useScale(logicalWidth: number, logicalHeight: number) {
  const hostRef = useRef<HTMLDivElement | null>(null);
  const [scale, setScale] = useState(0);

  useLayoutEffect(() => {
    const host = hostRef.current;
    if (!host) return;
    const measure = () => {
      const rect = host.getBoundingClientRect();
      if (!rect.width || !rect.height) return;
      setScale(Math.min(rect.width / logicalWidth, rect.height / logicalHeight));
    };
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(host);
    return () => observer.disconnect();
  }, [logicalWidth, logicalHeight]);

  return { hostRef, scale };
}

export function SlideView({ slide }: ViewProps) {
  const width = slide?.width ?? slide?.width_px ?? 1280;
  const height = slide?.height ?? slide?.height_px ?? 720;
  const { hostRef, scale } = useScale(width, height);
  const mediaBase = slide?.media_base ?? '';

  const canvasStyle: CSSProperties = {
    width,
    height,
    background: slide ? fillCss(slide.background) || '#fff' : '#fff',
    transform: `scale(${scale || 0.0001})`,
    visibility: scale ? 'visible' : 'hidden',
  };

  return (
    <div className="deck-host" ref={hostRef}>
      {!slide ? (
        <div className="deck-placeholder">
          <span>正在载入课件…</span>
        </div>
      ) : (
        <div className="deck-canvas" style={canvasStyle}>
          {(slide.decorations ?? []).map((element) => (
            <ElementNode key={element.element_id} element={element} mediaBase={mediaBase} />
          ))}
          {(slide.elements ?? []).map((element) => (
            <ElementNode key={element.element_id} element={element} mediaBase={mediaBase} />
          ))}
        </div>
      )}
    </div>
  );
}

function ElementNode({ element, mediaBase }: { element: DeckElement; mediaBase: string }) {
  const box = element.box ?? [0, 0, 0, 0];
  const [left, top, width, height] = box;
  const source = element.source === 'master' ? 0 : element.source === 'layout' ? 1000 : 100000;
  const style: CSSProperties = {
    position: 'absolute',
    left: (left || 0).toFixed(2) + 'px',
    top: (top || 0).toFixed(2) + 'px',
    width: Math.max(0, width || 0).toFixed(2) + 'px',
    height: Math.max(0, height || 0).toFixed(2) + 'px',
    zIndex: source + (element.z || 0),
  };
  if (element.rot) style.transform = `rotate(${element.rot}deg)`;

  if (element.kind === 'picture') {
    return (
      <div className="deck-el" style={style}>
        <PictureNode element={element} mediaBase={mediaBase} />
      </div>
    );
  }
  if (element.kind === 'table') {
    return (
      <div className="deck-el" style={style}>
        <TableNode element={element} />
      </div>
    );
  }
  if (element.kind === 'freeform' || element.kind === 'line' || element.kind === 'connector') {
    return (
      <div className="deck-el" style={style}>
        <SvgNode element={element} width={width} height={height} />
      </div>
    );
  }

  const hasText = Boolean(
    element.text && (element.text.paras ?? []).some((para) => (para.text || '').length || (para.runs ?? []).length),
  );
  const shapeStyle: CSSProperties = {
    position: 'absolute',
    inset: 0,
    background: fillCss(element.fill),
    ...roundedStyle(element.kind, element.preset, width, height),
  };
  const clip = clipFor(element.preset);
  if (clip) shapeStyle.clipPath = clip;
  const border = lineCss(element.line);
  if (border) shapeStyle.border = border;

  return (
    <div className="deck-el" style={style}>
      <div style={shapeStyle} />
      {hasText && <TextLayer element={element} />}
      {element.kind === 'unknown' && <div className="deck-unrender">未渲染：{element.name || element.preset || element.kind}</div>}
    </div>
  );
}

function TextLayer({ element }: { element: DeckElement }) {
  const text = element.text;
  if (!text) return null;
  const insets = text.insets_px && text.insets_px.length === 4 ? text.insets_px : [9.6, 4.8, 9.6, 4.8];
  const scale = text.font_scale && text.font_scale !== 1 ? text.font_scale : 1;
  const style: CSSProperties = {
    position: 'absolute',
    inset: 0,
    display: 'flex',
    flexDirection: 'column',
    justifyContent: text.anchor === 'middle' ? 'center' : text.anchor === 'bottom' ? 'flex-end' : 'flex-start',
    padding: insets.map((value) => (value * scale).toFixed(2) + 'px').join(' '),
    overflow: 'hidden',
  };
  return (
    <div style={style}>
      {(text.paras ?? []).map((para, index) => {
        const isBullet = para.bullet === 'char' || para.bullet === 'auto';
        const bullet = para.bullet_char || '•';
        const baseStyle = paraBaseStyle(para, scale);
        const pStyle = { ...paraStyle(para, scale), ...baseStyle };
        return (
          <div key={para.pid || index} style={pStyle}>
            {isBullet && (
              <span className="deck-bullet" style={{ fontFamily: fontStack(para.default_font) }}>
                {bullet}
              </span>
            )}
            {(para.runs && para.runs.length
              ? para.runs.map((run, runIndex) => (
                  <span key={runIndex} style={runStyle(run, scale)}>
                    {run.text || ''}
                  </span>
                ))
              : para.text || '')}
          </div>
        );
      })}
    </div>
  );
}

function PictureNode({ element, mediaBase }: { element: DeckElement; mediaBase: string }) {
  const pic = element.pic ?? {};
  const src = resolveImageSrc(pic.src, pic.external_url, mediaBase);
  const [failed, setFailed] = useState(false);
  const wrapStyle: CSSProperties = {
    position: 'absolute',
    inset: 0,
    background: failed ? undefined : '#eef1f6',
    display: 'flex',
    alignItems: 'center',
    justifyContent: 'center',
    overflow: 'hidden',
  };
  const border = lineCss(element.line);
  if (border) wrapStyle.border = border;

  if (!src || failed) {
    return (
      <div style={wrapStyle} className={failed ? 'deck-pic-missing' : undefined}>
        <span className="deck-unrender">
          {src ? '图片未取到：' + src.slice(0, 60) : '图片缺失'}
        </span>
      </div>
    );
  }

  const imgStyle: CSSProperties = { width: '100%', height: '100%', objectFit: 'fill', display: 'block' };
  const crop = pic.crop ?? [0, 0, 0, 0];
  if (crop.some((value) => Boolean(value))) {
    const [l, t, r, b] = crop;
    const kw = 1 - l - r;
    const kh = 1 - t - b;
    imgStyle.clipPath = `inset(${t * 100}% ${r * 100}% ${b * 100}% ${l * 100}%)`;
    imgStyle.width = `${100 / kw}%`;
    imgStyle.height = `${100 / kh}%`;
    imgStyle.marginLeft = `${(-l / kw) * 100}%`;
    imgStyle.marginTop = `${(-t / kh) * 100}%`;
    imgStyle.position = 'absolute';
  }
  return (
    <div style={wrapStyle}>
      <img src={src} alt={pic.alt || element.name || ''} style={imgStyle} loading="lazy" onError={() => setFailed(true)} />
    </div>
  );
}

function TableNode({ element }: { element: DeckElement }) {
  const table = element.table ?? {};
  return (
    <div className="deck-el" style={{ position: 'absolute', inset: 0 }}>
      <table className="deck-table" style={{ width: '100%', height: '100%', borderCollapse: 'collapse', tableLayout: 'fixed' }}>
        <colgroup>
          {(table.col_w_px ?? []).map((colWidth, index) => (
            <col key={index} style={{ width: colWidth }} />
          ))}
        </colgroup>
        <tbody>
          {(table.cells ?? []).map((row, rowIndex) => (
            <tr key={rowIndex} style={table.row_h_px && table.row_h_px[rowIndex] ? { height: table.row_h_px[rowIndex] } : undefined}>
              {row.map((cell, cellIndex) => (
                <CellNode key={cellIndex} cell={cell} />
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function CellNode({ cell }: { cell: DeckCell }) {
  const style: CSSProperties = { border: '1px solid #d8e0e8', verticalAlign: 'middle', padding: '4px 8px' };
  const fill: DeckFill | null | undefined = cell.fill;
  if (fill && fill.kind !== 'none') style.background = fillCss(fill);
  return (
    <td
      colSpan={cell.col_span && cell.col_span > 1 ? cell.col_span : undefined}
      rowSpan={cell.row_span && cell.row_span > 1 ? cell.row_span : undefined}
      style={style}
    >
      {(cell.paras ?? []).map((para, index) => (
        <div key={para.pid || index} style={{ ...paraBaseStyle(para, 1), ...paraStyle(para, 1) }}>
          {(para.runs ?? []).map((run, runIndex) => (
            <span key={runIndex} style={runStyle(run, 1)}>
              {run.text || ''}
            </span>
          ))}
          {!para.runs?.length && para.text}
        </div>
      ))}
      {!cell.paras?.length && (cell.text || '')}
    </td>
  );
}

function SvgNode({ element, width, height }: { element: DeckElement; width: number; height: number }) {
  const gradientId = useId().replace(/:/g, '');
  const fill = element.fill;
  const line = element.line;
  const path = element.svg_path || '';
  const usesGradient = Boolean(fill && fill.kind === 'gradient' && fill.gradient && fill.gradient.length);
  const fillPaint = usesGradient ? `url(#${gradientId})` : fill && fill.kind !== 'none' && fill.kind !== 'image' ? fillCss(fill) : 'none';
  const stroke = line && line.kind !== 'none' && line.width_px ? cssColor(line.color, line.alpha) : 'none';

  return (
    <svg
      viewBox={`0 0 ${Math.max(1, width)} ${Math.max(1, height)}`}
      preserveAspectRatio="none"
      style={{ position: 'absolute', inset: 0, width: '100%', height: '100%' }}
    >
      {usesGradient && fill?.gradient && (
        <defs>
          <linearGradient
            id={gradientId}
            x1={0.5 - Math.sin(((fill.angle || 0) * Math.PI) / 180) / 2}
            y1={0.5 + Math.cos(((fill.angle || 0) * Math.PI) / 180) / 2}
            x2={0.5 + Math.sin(((fill.angle || 0) * Math.PI) / 180) / 2}
            y2={0.5 - Math.cos(((fill.angle || 0) * Math.PI) / 180) / 2}
          >
            {fill.gradient.map((stop, index) => (
              <stop key={index} offset={`${Math.round((stop.pos || 0) * 100)}%`} stopColor={cssColor(stop.color, stop.alpha)} />
            ))}
          </linearGradient>
        </defs>
      )}
      {path ? (
        <path
          d={path}
          fill={fillPaint}
          stroke={stroke}
          strokeWidth={line && line.width_px ? line.width_px : undefined}
          strokeLinejoin="round"
          strokeLinecap={line?.cap === 'round' ? 'round' : 'butt'}
          strokeDasharray={line?.dash ? (line.dash.indexOf('dot') >= 0 ? '2 3' : '6 4') : undefined}
        />
      ) : (
        <line x1={0} y1={0} x2={width} y2={height} stroke={stroke === 'none' ? '#888' : stroke} strokeWidth={(line && line.width_px) || 1} />
      )}
    </svg>
  );
}
