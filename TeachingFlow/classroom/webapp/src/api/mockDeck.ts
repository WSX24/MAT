/** VITE_MOCK=1 的内置课件：形状与 deck/schema.py 的 JSON 完全一致。 */
import type { DeckElement, DeckSlide } from '../features/deck/types';

function t(
  id: string,
  box: [number, number, number, number],
  paras: { text: string; size: number; color: string; bold?: boolean; bullet?: 'none' | 'char'; align?: string; spacing?: number }[],
  opts: { z?: number; anchor?: string; insets?: number[] } = {},
): DeckElement {
  return {
    element_id: id,
    kind: 'text',
    role: 'body',
    box,
    z: opts.z ?? 10,
    fill: null,
    line: null,
    text: {
      anchor: opts.anchor ?? 'top',
      wrap: true,
      font_scale: 1,
      insets_px: opts.insets ?? [9.6, 4.8, 9.6, 4.8],
      paras: paras.map((p, i) => ({
        pid: id + '-p' + (i + 1),
        text: p.text,
        level: 0,
        align: p.align ?? 'left',
        bullet: p.bullet ?? 'none',
        bullet_char: p.bullet === 'char' ? '•' : '',
        line_spacing: p.spacing ?? 1.35,
        space_after_pt: 6,
        indent_px: 0,
        margin_left_px: p.bullet === 'char' ? 22 : 0,
        first_line_px: 0,
        default_size_pt: p.size,
        default_color: p.color,
        default_font: 'MiSans',
        default_bold: p.bold ?? null,
        runs: [
          {
            text: p.text,
            bold: p.bold ?? null,
            italic: null,
            underline: null,
            strike: null,
            size_pt: p.size,
            color: p.color,
            font: 'MiSans',
            font_ea: 'MiSans',
            baseline: '',
            spacing_pt: null,
          },
        ],
      })),
    },
  };
}

function rect(
  id: string,
  box: [number, number, number, number],
  fill: { color: string; alpha?: number },
  preset = 'rect',
  z = 1,
): DeckElement {
  return {
    element_id: id,
    kind: 'rect',
    role: 'decor',
    box,
    z,
    preset,
    fill: { kind: 'solid', color: fill.color, alpha: fill.alpha ?? 1, gradient: [], angle: 0, image: '' },
    line: { kind: 'none', color: '#000000', alpha: 1, width_px: 1, dash: '', cap: 'flat' },
    text: null,
    pic: null,
    table: null,
    svg_path: '',
    decorative: true,
  };
}

const slideTable: DeckElement = {
  element_id: 's04-table',
  kind: 'table',
  role: 'table',
  box: [90, 200, 1100, 380],
  z: 20,
  fill: null,
  line: null,
  text: null,
  pic: null,
  svg_path: '',
  preset: '',
  table: {
    col_w_px: [240, 430, 430],
    row_h_px: [70, 100, 100, 100],
    cells: [
      [
        { text: '比较项', fill: { kind: 'solid', color: '#1D5C4D', alpha: 1, gradient: [], angle: 0, image: '' } },
        { text: '位移', fill: { kind: 'solid', color: '#1D5C4D', alpha: 1, gradient: [], angle: 0, image: '' } },
        { text: '路程', fill: { kind: 'solid', color: '#1D5C4D', alpha: 1, gradient: [], angle: 0, image: '' } },
      ],
      [{ text: '定义' }, { text: '从初位置指向末位置的有向线段' }, { text: '物体运动轨迹的长度' }],
      [{ text: '性质' }, { text: '矢量（有大小、有方向）' }, { text: '标量（只有大小）' }],
      [{ text: '单位' }, { text: '米（m）' }, { text: '米（m）' }],
    ].map((row, rowIndex) =>
      row.map((cell, cellIndex) => ({
        ...cell,
        paras: [
          {
            pid: `cell-r${rowIndex}c${cellIndex}`,
            text: cell.text,
            runs: [
              {
                text: cell.text,
                bold: false,
                size_pt: 16,
                color: (cell as { fill?: unknown }).fill ? '#FFFFFF' : '#1f2d3d',
                font: 'MiSans',
                font_ea: 'MiSans',
                baseline: '',
              },
            ],
            level: 0,
            align: 'left',
            bullet: 'none',
            line_spacing: 1.2,
            indent_px: 0,
            margin_left_px: 0,
            first_line_px: 0,
            default_size_pt: 16,
            default_color: (cell as { fill?: unknown }).fill ? '#FFFFFF' : '#1f2d3d',
            default_font: 'MiSans',
            default_bold: null,
          },
        ],
      })),
    ),
  },
};

export const MOCK_SLIDES: DeckSlide[] = [
  {
    index: 0,
    title: '封面',
    width: 1280,
    height: 720,
    background: { kind: 'solid', color: '#F7F4ED', alpha: 1, gradient: [], angle: 0, image: '' },
    media_base: '',
    decorations: [
      rect('s01-sh01', [826.67, 80, 440, 440], { color: '#1D5C4D', alpha: 0.16 }, 'ellipse', 1),
      rect('s01-sh02', [933.33, 186.67, 320, 320], { color: '#1D5C4D', alpha: 0.22 }, 'ellipse', 2),
      rect('s01-sh03', [1061.33, 88, 45.33, 45.33], { color: '#E06B2D', alpha: 1 }, 'ellipse', 3),
      rect('s01-sh04', [80, 560, 64, 6], { color: '#E06B2D', alpha: 1 }, 'rect', 4),
    ],
    elements: [
      t('s01-sh05', [93.33, 170.67, 666.67, 40], [{ text: '高中物理 · 暑期衔接 · 第一课', size: 14, color: '#1D5C4D', bold: true }]),
      t('s01-sh06', [93.33, 220, 700, 160], [{ text: '运动的描述', size: 54, color: '#14232b', bold: true }], { z: 11 }),
      t('s01-sh07', [93.33, 380, 640, 120], [
        { text: '质点 · 参考系 · 位移与路程', size: 20, color: '#41525f' },
        { text: '预计 12 分钟 · 共 5 页', size: 16, color: '#7b8798' },
      ], { z: 12 }),
    ],
  },
  {
    index: 1,
    title: '本节目标',
    width: 1280,
    height: 720,
    background: { kind: 'solid', color: '#FFFFFF', alpha: 1, gradient: [], angle: 0, image: '' },
    media_base: '',
    decorations: [rect('s02-sh01', [0, 0, 1280, 96], { color: '#1D5C4D', alpha: 1 }, 'rect', 1)],
    elements: [
      t('s02-sh02', [80, 22, 900, 56], [{ text: '本节目标', size: 28, color: '#FFFFFF', bold: true }], { z: 5 }),
      t('s02-sh03', [96, 160, 1080, 420], [
        { text: '说出什么是质点，并判断什么情况下可以把物体看成质点。', size: 22, color: '#1f2d3d', bullet: 'char' },
        { text: '区分参考系与坐标系，会用坐标描述位置。', size: 22, color: '#1f2d3d', bullet: 'char' },
        { text: '说出位移与路程的区别，会计算一维运动的位移。', size: 22, color: '#1f2d3d', bullet: 'char' },
      ], { z: 6 }),
      rect('s02-sh04', [96, 150, 8, 300], { color: '#E06B2D', alpha: 1 }, 'rect', 4),
    ],
  },
  {
    index: 2,
    title: '质点与参考系',
    width: 1280,
    height: 720,
    background: { kind: 'solid', color: '#FFFFFF', alpha: 1, gradient: [], angle: 0, image: '' },
    media_base: '',
    decorations: [],
    elements: [
      t('s03-sh01', [80, 60, 800, 60], [{ text: '质点：把物体简化成一个有质量的点', size: 26, color: '#1D5C4D', bold: true }]),
      t('s03-sh02', [80, 130, 560, 300], [
        { text: '地球绕太阳公转，地球的大小和形状对研究的问题影响很小，可以把地球看成质点。', size: 18, color: '#33404d' },
        { text: '研究地球自转时，各点的运动情况不同，就不能再看成质点。', size: 18, color: '#33404d' },
      ]),
      {
        element_id: 's03-pic',
        kind: 'picture',
        role: 'picture',
        box: [700, 150, 480, 300],
        z: 12,
        fill: null,
        line: { kind: 'solid', color: '#d8e0e8', alpha: 1, width_px: 1, dash: '', cap: 'flat' },
        text: null,
        svg_path: '',
        pic: {
          src: 'data:image/svg+xml;charset=utf-8,<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 320 200"><rect width="320" height="200" fill="%23fff" rx="8"/><path d="M40 20 V170 H300" stroke="%231D5C4D" stroke-width="2" fill="none"/><path d="M40 170 L60 170 M40 120 H300" stroke="%23c9d2d9" stroke-dasharray="4 4"/><path d="M90 60 L150 100 L210 100 L260 60" stroke="%23E06B2D" stroke-width="3" fill="none"/><circle cx="150" cy="100" r="5" fill="%231D5C4D"/><text x="52" y="40" font-size="12" fill="%231D5C4D">x/m</text><text x="6" y="176" font-size="12" fill="%231D5C4D">t/s</text></svg>',
          external_url: '',
          natural_w: 320,
          natural_h: 200,
          crop: [0, 0, 0, 0],
          alt: '一幅把位置随时间变化画成折线的示意图',
        },
      },
      t('s03-sh04', [80, 470, 1120, 120], [{ text: '参考系：描述运动时选作参照的物体。选不同的参考系，同一运动的描述可能不同。', size: 18, color: '#33404d' }]),
    ],
  },
  {
    index: 3,
    title: '位移与路程',
    width: 1280,
    height: 720,
    background: { kind: 'solid', color: '#FFFFFF', alpha: 1, gradient: [], angle: 0, image: '' },
    media_base: '',
    decorations: [rect('s04-sh01', [80, 120, 120, 6], { color: '#E06B2D', alpha: 1 }, 'rect', 2)],
    elements: [
      t('s04-sh02', [80, 50, 800, 60], [{ text: '位移与路程：一字之差，物理意义不同', size: 26, color: '#1D5C4D', bold: true }]),
      slideTable,
    ],
  },
  {
    index: 4,
    title: '小结',
    width: 1280,
    height: 720,
    background: { kind: 'solid', color: '#F7F4ED', alpha: 1, gradient: [], angle: 0, image: '' },
    media_base: '',
    decorations: [rect('s05-sh01', [900, 420, 300, 220], { color: '#1D5C4D', alpha: 0.12 }, 'ellipse', 1)],
    elements: [
      t('s05-sh02', [80, 80, 800, 60], [{ text: '这一节记住三件事', size: 30, color: '#14232b', bold: true }]),
      t('s05-sh03', [80, 180, 1060, 380], [
        { text: '能不能看成质点，取决于研究的问题，不取决于物体大小。', size: 22, color: '#1f2d3d', bullet: 'char' },
        { text: '位移是矢量，路程是标量；往返运动中两者大小通常不相等。', size: 22, color: '#1f2d3d', bullet: 'char' },
        { text: '先定参考系，再谈运动——否则描述没有意义。', size: 22, color: '#1f2d3d', bullet: 'char' },
      ]),
      t('s05-sh04', [80, 600, 900, 60], [{ text: '课后：用坐标系画出你从家到学校的位移与路程。', size: 18, color: '#41525f' }]),
    ],
  },
];

/** 每页要"讲"的句子（mock 课堂按此逐句播报）。 */
export const MOCK_SPEECH: string[][] = [
  [
    '欢迎来到第一课，运动的描述。这五页会带你把质点和位移这两件事想清楚。',
    '先看今天的目标：什么叫质点，参考系怎么选，位移与路程差在哪里。',
  ],
  [
    '第一，说出什么是质点，并判断什么情况下可以把物体看成质点。',
    '第二，区分参考系与坐标系，会用坐标描述位置。',
    '第三，说出位移与路程的区别，会计算一维运动的位移。',
    '这三条是后面整章的地基，任何一条含糊，做题都会慢半拍。',
  ],
  [
    '我们先看质点。质点不是"小物体"，而是把物体简化成一个有质量的点。',
    '地球绕太阳公转时，地球的直径相对轨道半径可以忽略，这时地球就可以看成质点。',
    '但研究地球自转时，各点运动情况不同，地球就不能再看成质点。',
    '所以判断标准是：物体的大小和形状对所研究的问题有没有影响。',
    '再看参考系。描述运动必须选参照物，选不同参考系，同一运动的描述可能不同。',
  ],
  [
    '这一页把位移和路程放在一起对照。',
    '位移是从初位置指向末位置的有向线段，是矢量，有大小也有方向。',
    '路程是运动轨迹的长度，是标量，只有大小。',
    '一个人绕操场跑一圈回到起点，路程是四百米，位移是零。',
  ],
  [
    '最后把这一节收一下。',
    '能不能看成质点，取决于研究的问题，不取决于物体大小。',
    '位移是矢量，路程是标量，往返运动中两者大小通常不相等。',
    '先定参考系，再谈运动，否则描述没有意义。',
    '课后请用坐标系画出你从家到学校的位移与路程，下节课我们对照。',
  ],
];
