import { Link, useLocation, useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/client';
import type { Recap, SessionDetail, UnitDetail } from '../api/types';
import { Button } from '../components/Button';
import { Empty } from '../components/Empty';
import { IconArrowLeft, IconCheck, IconPlay } from '../components/icons';
import { SkeletonRows } from '../components/Skeleton';
import { useAsync } from '../hooks/useAsync';
import { formatClock } from '../lib/format';

interface SummaryState {
  recap?: Recap;
  unitId?: string;
}

export function SummaryPage() {
  const { id = '' } = useParams();
  const location = useLocation();
  const navigate = useNavigate();
  const state = (location.state ?? null) as SummaryState | null;

  const data = useAsync(async () => {
    const recap = state?.recap ?? (await api.getSessionRecap(id));
    let session: SessionDetail | null = null;
    try {
      session = await api.getSession(id);
    } catch {
      session = null;
    }
    const unitId = session?.unit_id ?? state?.unitId ?? null;
    let unit: UnitDetail | null = null;
    if (unitId) {
      try {
        unit = await api.getUnit(unitId);
      } catch {
        unit = null;
      }
    }
    return { recap, session, unit };
  }, [id]);

  const recap = data.data?.recap ?? null;
  const unit = data.data?.unit ?? null;
  const courseLink = unit ? `/courses/${unit.course_id}` : '/';
  const learnLink = unit ? `/learn/${unit.id}` : '/';

  return (
    <div className="page page--narrow">
      <div className="page-head">
        <div className="page-head__main">
          <div className="page-head__eyebrow">
            <span>课后小结</span>
          </div>
          <h1 className="page-head__title">这一节上完了</h1>
          <p className="page-head__sub">
            {unit ? `${unit.title} · ` : ''}下面是这次课堂的记录。随时可以回到同一单元继续，进度接着往后走。
          </p>
        </div>
      </div>

      {data.status === 'loading' && <SkeletonRows count={2} />}

      {data.status === 'error' && (
        <Empty
          art="error"
          error
          title="小结没能加载出来"
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

      {data.status === 'ready' && recap && (
        <>
          <div className="summary-grid">
            <div className="stat">
              <div className="stat__value">{formatClock(recap.elapsed_sec)}</div>
              <div className="stat__label">课堂时长</div>
            </div>
            <div className="stat">
              <div className="stat__value">{recap.slides_covered}</div>
              <div className="stat__label">覆盖页数</div>
            </div>
            <div className="stat">
              <div className="stat__value">{recap.units_given}</div>
              <div className="stat__label">讲解单元</div>
            </div>
            <div className="stat">
              <div className="stat__value">{recap.qa_count}</div>
              <div className="stat__label">提问次数</div>
            </div>
          </div>

          <div className="card summary-card">
            <div className="row" style={{ gap: 'var(--space-2)' }}>
              <IconCheck size={18} />
              <h2 style={{ fontSize: 'var(--text-lg)' }}>这次讲到的主题</h2>
            </div>
            {recap.topics?.length ? (
              <div className="chips">
                {recap.topics.map((topic) => (
                  <span className="chip" key={topic}>
                    {topic}
                  </span>
                ))}
              </div>
            ) : (
              <p className="muted" style={{ fontSize: 'var(--text-sm)' }}>
                这一节还没有记录到主题标签。
              </p>
            )}
            <div className="summary-actions">
              <Button variant="primary" iconLeft={<IconPlay size={16} />} onClick={() => navigate(learnLink)}>
                继续学这一单元
              </Button>
              <Link to={courseLink} className="btn btn--secondary">
                返回课程
              </Link>
              <Link to="/" className="btn btn--ghost">
                <IconArrowLeft size={16} /> 课程库
              </Link>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
