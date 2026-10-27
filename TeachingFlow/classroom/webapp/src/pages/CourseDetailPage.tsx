import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/client';
import type { CourseUnit } from '../api/types';
import { Badge } from '../components/Badge';
import { Button } from '../components/Button';
import { Empty } from '../components/Empty';
import { IconArrowLeft, IconChevronRight, IconPages, IconPlay } from '../components/icons';
import { ProgressBar } from '../components/Progress';
import { SkeletonRows } from '../components/Skeleton';
import { useToast } from '../components/Toast';
import { useAsync } from '../hooks/useAsync';
import { errorText, formatEstimate, sourceLabel, statusLabel } from '../lib/format';

export function CourseDetailPage() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const course = useAsync(() => api.getCourse(id), [id]);
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set());
  const [preparing, setPreparing] = useState<string | null>(null);

  const toggleChapter = (index: number) => {
    setCollapsed((current) => {
      const next = new Set(current);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });
  };

  const prepare = async (unit: CourseUnit): Promise<boolean> => {
    if (unit.ready) return true;
    setPreparing(unit.id);
    try {
      const result = await api.prepareUnit(unit.id);
      const warnings = result.warnings ?? [];
      if (warnings.length) toast.warn(warnings[0]);
      else toast.ok(`「${unit.title}」备课完成`);
      course.reload();
      return result.plan_ready;
    } catch (error) {
      toast.error(errorText(error));
      return false;
    } finally {
      setPreparing(null);
    }
  };

  const startUnit = async (unit: CourseUnit) => {
    const ready = await prepare(unit);
    if (ready) navigate(`/learn/${unit.id}`);
  };

  const chapters = course.data?.chapters ?? [];
  const totalUnits = chapters.reduce((sum, chapter) => sum + chapter.units.length, 0);
  const readyUnits = chapters.reduce((sum, chapter) => sum + chapter.units.filter((unit) => unit.ready).length, 0);
  const allCollapsed = chapters.length > 0 && chapters.every((_, index) => collapsed.has(index));

  return (
    <div className="page">
      <div className="page-head">
        <div className="page-head__main">
          <div className="page-head__eyebrow">
            <Link to="/" className="topnav__link" style={{ paddingLeft: 0 }}>
              课程库
            </Link>
            <span aria-hidden="true">/</span>
            <span>{course.data ? `${course.data.subject || '未分类'} · ${sourceLabel(course.data.source)}` : '课程详情'}</span>
          </div>
          <h1 className="page-head__title">{course.data?.title || (course.status === 'loading' ? '正在加载…' : '课程')}</h1>
          <p className="page-head__sub">
            {course.data
              ? `${chapters.length} 章 · ${totalUnits} 个单元 · ${readyUnits} 个已备课`
              : '按章折叠，点「开始 / 继续」直接进课堂。'}
          </p>
        </div>
        <div className="page-head__actions">
          <Link to="/" className="btn btn--ghost btn--sm" aria-label="返回课程库">
            <IconArrowLeft size={18} /> 课程库
          </Link>
          {chapters.length > 0 && (
            <Button
              variant="secondary"
              size="sm"
              onClick={() =>
                setCollapsed(allCollapsed ? new Set() : new Set(chapters.map((_, index) => index)))
              }
            >
              {allCollapsed ? '全部展开' : '全部收起'}
            </Button>
          )}
        </div>
      </div>

      {course.status === 'loading' && <SkeletonRows count={3} />}

      {course.status === 'error' && (
        <Empty
          art="error"
          error
          title="课程没能打开"
          description={course.error || '请稍后重试。'}
          actions={
            <>
              <Button variant="primary" onClick={course.reload}>
                重试
              </Button>
              <Link to="/" className="btn btn--secondary">
                返回课程库
              </Link>
            </>
          }
        />
      )}

      {course.status === 'ready' && chapters.length === 0 && (
        <Empty
          art="courses"
          title="这门课还没有单元"
          description="可能是课件还在解析，或者 runs 目录里缺少 pptx。回课程库点「重新扫描」再看。"
          actions={
            <Link to="/" className="btn btn--primary">
              返回课程库
            </Link>
          }
        />
      )}

      {course.status === 'ready' && chapters.length > 0 && (
        <div className="chapters">
          {chapters.map((chapter, index) => {
            const isOpen = !collapsed.has(index);
            return (
              <section className="chapter" key={chapter.title + index}>
                <h2 style={{ margin: 0 }}>
                  <button
                    type="button"
                    className="chapter__toggle"
                    aria-expanded={isOpen}
                    aria-controls={`chapter-body-${index}`}
                    aria-label={`${chapter.title}（${chapter.units.length} 个单元）${isOpen ? '，点击收起' : '，点击展开'}`}
                    onClick={() => toggleChapter(index)}
                  >
                    <span className="chapter__chevron" aria-hidden="true">
                      <IconChevronRight size={18} />
                    </span>
                    <span className="chapter__name">{chapter.title}</span>
                    <span className="chapter__count">
                      {chapter.units.length} 个单元 · {chapter.units.filter((unit) => unit.ready).length} 已备课
                    </span>
                  </button>
                </h2>
                {isOpen && (
                  <div className="chapter__body" id={`chapter-body-${index}`}>
                    {chapter.units.map((unit, unitIndex) => {
                      const status = statusLabel(unit.progress?.status);
                      const percent = Math.max(0, Math.min(100, Math.round(unit.progress?.percent || 0)));
                      const cta = percent >= 100 ? '重学' : percent > 0 ? '继续' : '开始';
                      return (
                        <div className="unit-row" key={unit.id}>
                          <div className="unit-row__index" aria-hidden="true">
                            {String(unitIndex + 1).padStart(2, '0')}
                          </div>
                          <div>
                            <div className="unit-row__title">{unit.title}</div>
                            <div className="unit-row__meta">
                              <span>
                                <IconPages size={15} /> {unit.slides} 页
                              </span>
                              <span>{formatEstimate(unit.est_sec)}</span>
                              <Badge tone={status.tone}>{status.label}</Badge>
                              {!unit.ready && <Badge tone="warn">未备课</Badge>}
                              {unit.ready && unit.plan_source === 'rule' && <Badge tone="outline">规则兜底</Badge>}
                            </div>
                          </div>
                          <div className="unit-row__progress">
                            <span>{percent > 0 ? `已学 ${percent}%` : '还没开始'}</span>
                            <ProgressBar value={percent} size="sm" label={`${unit.title} 学习进度`} />
                          </div>
                          <div className="unit-row__actions">
                            <Link className="btn btn--secondary btn--sm" to={`/units/${unit.id}/outline`}>
                              看大纲
                            </Link>
                            <Button
                              variant="primary"
                              size="sm"
                              loading={preparing === unit.id}
                              onClick={() => void startUnit(unit)}
                              iconLeft={<IconPlay size={14} />}
                              aria-label={`${cta} ${unit.title}`}
                            >
                              {unit.ready ? cta : '备课并开始'}
                            </Button>
                          </div>
                        </div>
                      );
                    })}
                  </div>
                )}
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}
