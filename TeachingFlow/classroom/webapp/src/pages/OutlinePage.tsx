import { useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/client';
import { Badge } from '../components/Badge';
import { Button } from '../components/Button';
import { Empty } from '../components/Empty';
import { IconArrowLeft, IconPlay } from '../components/icons';
import { SkeletonRows } from '../components/Skeleton';
import { useToast } from '../components/Toast';
import { useAsync } from '../hooks/useAsync';
import { errorText, formatEstimate } from '../lib/format';

export function OutlinePage() {
  const { id = '' } = useParams();
  const navigate = useNavigate();
  const toast = useToast();
  const data = useAsync(async () => {
    const [unit, outline] = await Promise.all([api.getUnit(id), api.getUnitOutline(id)]);
    return { unit, outline };
  }, [id]);
  const [preparing, setPreparing] = useState(false);

  const unit = data.data?.unit ?? null;
  const outline = data.data?.outline ?? null;
  const pages = outline?.pages ?? [];
  const backTo = unit ? `/courses/${unit.course_id}` : '/';

  const prepare = async () => {
    setPreparing(true);
    try {
      const result = await api.prepareUnit(id);
      const warnings = result.warnings ?? [];
      if (warnings.length) toast.warn(warnings[0]);
      else toast.ok('备课完成，大纲已生成');
      data.reload();
    } catch (error) {
      toast.error(errorText(error));
    } finally {
      setPreparing(false);
    }
  };

  return (
    <div className="page page--narrow">
      <div className="page-head">
        <div className="page-head__main">
          <div className="page-head__eyebrow">
            <Link to={backTo} className="topnav__link" style={{ paddingLeft: 0 }}>
              课程
            </Link>
            <span aria-hidden="true">/</span>
            <span>单元大纲</span>
          </div>
          <h1 className="page-head__title">{unit?.title || (data.status === 'loading' ? '正在加载…' : '大纲预览')}</h1>
          <p className="page-head__sub">
            这一单元每页要讲什么、讲多久。可以从任意一页开始学，进度会从那里往后记。
          </p>
        </div>
        <div className="page-head__actions">
          <Link to={backTo} className="btn btn--ghost btn--sm" aria-label="返回课程">
            <IconArrowLeft size={18} /> 课程
          </Link>
          {unit && pages.length > 0 && (
            <Button variant="primary" iconLeft={<IconPlay size={16} />} onClick={() => navigate(`/learn/${unit.id}`)}>
              开始学习
            </Button>
          )}
          {unit && pages.length === 0 && (
            <Button variant="primary" loading={preparing} onClick={() => void prepare()}>
              备课并生成大纲
            </Button>
          )}
        </div>
      </div>

      {data.status === 'loading' && <SkeletonRows count={3} />}

      {data.status === 'error' && (
        <Empty
          art="error"
          error
          title="大纲没能加载出来"
          description={data.error || '请稍后重试。'}
          actions={
            <>
              <Button variant="primary" onClick={data.reload}>
                重试
              </Button>
              <Link to="/" className="btn btn--secondary">
                返回课程库
              </Link>
            </>
          }
        />
      )}

      {data.status === 'ready' && outline && (
        <>
          <div className="outline-totals">
            <div className="outline-total">
              <div className="outline-total__value">{outline.totals.slides}</div>
              <div className="outline-total__label">页课件</div>
            </div>
            <div className="outline-total">
              <div className="outline-total__value">{outline.totals.units}</div>
              <div className="outline-total__label">个讲解单元</div>
            </div>
            <div className="outline-total">
              <div className="outline-total__value">{formatEstimate(outline.totals.est_sec)}</div>
              <div className="outline-total__label">预计时长</div>
            </div>
          </div>

          {pages.length === 0 ? (
            <Empty
              art="outline"
              title="这个单元还没有大纲"
              description="备课（解析课件 + 生成讲解脚本）完成后，这里会按页列出一句话概要、预计时长和知识点。"
              actions={
                <>
                  <Button variant="primary" loading={preparing} onClick={() => void prepare()}>
                    备课并生成大纲
                  </Button>
                  <Link to={backTo} className="btn btn--secondary">
                    返回课程
                  </Link>
                </>
              }
            />
          ) : (
            <div className="outline-pages">
              {pages.map((page) => (
                <section className="outline-page" key={page.index}>
                  <div className="outline-page__head">
                    <span className="outline-page__no">P{page.index + 1}</span>
                    <span className="outline-page__title">{page.title || `第 ${page.index + 1} 页`}</span>
                    <Button
                      variant="secondary"
                      size="sm"
                      className="outline-page__start"
                      onClick={() => navigate(`/learn/${id}?slide=${page.index}`)}
                      aria-label={`从第 ${page.index + 1} 页开始学习`}
                    >
                      从这里开始
                    </Button>
                  </div>
                  <div className="outline-units">
                    {page.units.map((unitEntry, unitIndex) => (
                      <div className="outline-unit" key={unitIndex}>
                        <p className="outline-unit__preview">{unitEntry.preview}</p>
                        <div className="outline-unit__meta">
                          <span>预计 {Math.round(unitEntry.est_sec)} 秒</span>
                          {unitEntry.kp?.length > 0 && (
                            <div className="chips">
                              {unitEntry.kp.map((point) => (
                                <span className="chip" key={point}>
                                  {point}
                                </span>
                              ))}
                            </div>
                          )}
                        </div>
                      </div>
                    ))}
                  </div>
                </section>
              ))}
            </div>
          )}

          {unit && unit.plan_source === 'rule' && (
            <div className="banner banner--warn" style={{ marginTop: 'var(--space-5)' }}>
              <Badge tone="warn">规则兜底</Badge>
              <span>这份大纲是规则生成的（没有模型可用）。内容能上课，但讲解不会像模型版那么细。</span>
            </div>
          )}
        </>
      )}
    </div>
  );
}
