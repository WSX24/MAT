import { useCallback, useEffect, useRef, useState } from 'react';
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom';
import { api } from '../api/client';
import type { Recap, SessionCreated, UnitDetail } from '../api/types';
import { Badge } from '../components/Badge';
import { Button } from '../components/Button';
import { Empty } from '../components/Empty';
import { IconArrowLeft, IconVolume } from '../components/icons';
import { SkeletonRows } from '../components/Skeleton';
import { useToast } from '../components/Toast';
import { ControlBar } from '../features/classroom/ControlBar';
import { TranscriptPanel } from '../features/classroom/TranscriptPanel';
import { useClassroom } from '../features/classroom/useClassroom';
import { SlideView } from '../features/deck/SlideView';
import { useAsync } from '../hooks/useAsync';
import { errorText, formatClock } from '../lib/format';
import { useApp } from '../state/AppState';

export function ClassroomPage() {
  const { unitId = '' } = useParams();
  const [search] = useSearchParams();
  const { activeProfile, profilesStatus, profilesError, reloadProfiles } = useApp();
  const rawSlide = search.get('slide');
  const parsedSlide = rawSlide === null ? null : Number.parseInt(rawSlide, 10);
  const startSlide = parsedSlide !== null && Number.isFinite(parsedSlide) ? Math.max(0, parsedSlide) : null;

  if (!activeProfile) {
    return (
      <div className="page page--narrow">
        {profilesStatus === 'error' ? (
          <Empty
            art="error"
            error
            title="档案没加载出来，进不了课堂"
            description={profilesError || '请确认后端服务在运行。'}
            actions={
              <Button variant="primary" onClick={() => void reloadProfiles()}>
                重试
              </Button>
            }
          />
        ) : (
          <SkeletonRows count={2} />
        )}
      </div>
    );
  }

  return (
    <ClassroomSessionView
      key={`${unitId}:${startSlide ?? 'resume'}:${activeProfile.id}`}
      unitId={unitId}
      profileId={activeProfile.id}
      startSlide={startSlide}
      profileName={activeProfile.name}
    />
  );
}

interface SessionViewProps {
  unitId: string;
  profileId: string;
  startSlide: number | null;
  profileName: string;
}

/** 开发模式 StrictMode 会把 effect 跑两遍：共享同一个在建会话，避免后端多建一个空会话。 */
const bootInFlight = new Map<string, Promise<{ unit: UnitDetail; session: SessionCreated }>>();

function bootClassroom(unitId: string, profileId: string, startSlide: number | null) {
  const key = `${unitId}:${profileId}:${startSlide ?? 'resume'}`;
  const existing = bootInFlight.get(key);
  if (existing) return existing;
  const promise = (async () => {
    const unit = await api.getUnit(unitId);
    if (!unit.ready) await api.prepareUnit(unitId);
    const session = await api.createSession({
      unit_id: unitId,
      profile_id: profileId,
      start_slide: startSlide ?? undefined,
    });
    return { unit, session };
  })();
  bootInFlight.set(key, promise);
  const cleanup = () => bootInFlight.delete(key);
  promise.then(cleanup, cleanup);
  return promise;
}

function ClassroomSessionView({ unitId, profileId, startSlide, profileName }: SessionViewProps) {
  const boot = useAsync(() => bootClassroom(unitId, profileId, startSlide), [unitId, profileId, startSlide]);

  if (boot.status === 'loading') {
    return (
      <div className="page page--narrow">
        <SkeletonRows count={2} />
      </div>
    );
  }

  if (boot.status === 'error' || !boot.data) {
    return (
      <div className="page page--narrow">
        <Empty
          art="error"
          error
          title="课堂没能打开"
          description={boot.error || '请稍后重试。'}
          actions={
            <>
              <Button variant="primary" onClick={boot.reload}>
                重试
              </Button>
              <Link to={`/units/${unitId}/outline`} className="btn btn--secondary">
                先看大纲
              </Link>
            </>
          }
        />
      </div>
    );
  }

  return (
    <ClassroomLive
      unit={boot.data.unit}
      session={boot.data.session}
      profileName={profileName}
      startSlide={startSlide}
    />
  );
}

interface ClassroomLiveProps {
  unit: UnitDetail;
  session: SessionCreated;
  profileName: string;
  startSlide: number | null;
}

function ClassroomLive({ unit, session, profileName, startSlide }: ClassroomLiveProps) {
  const navigate = useNavigate();
  const toast = useToast();
  const startedAt = useRef(Date.now());
  const navigated = useRef(false);
  const [elapsed, setElapsed] = useState(0);
  const [finishing, setFinishing] = useState(false);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const goSummary = useCallback(
    (recap: Recap) => {
      if (navigated.current) return;
      navigated.current = true;
      navigate(`/sessions/${session.session_id}/summary`, {
        state: { recap, unitId: unit.id, profileName },
        replace: true,
      });
    },
    [navigate, session.session_id, unit.id, profileName],
  );

  const classroom = useClassroom({
    sessionId: session.session_id,
    startSlide: startSlide ?? session.position?.slide ?? 0,
    onRecap: goSummary,
  });

  const { control, paused, status, retries, slide, slideIndex, slideCount, caption, rate, transcript, autoplayBlocked } =
    classroom;

  useEffect(() => {
    const timer = window.setInterval(() => setElapsed(Math.round((Date.now() - startedAt.current) / 1000)), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    if (autoplayBlocked) toast.warn('浏览器挡住了自动播放：点右上角「播放声音」继续');
  }, [autoplayBlocked, toast]);

  const togglePause = useCallback(() => control(paused ? 'resume' : 'pause'), [control, paused]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null;
      if (target && (target.tagName === 'INPUT' || target.tagName === 'TEXTAREA' || target.isContentEditable)) return;
      if (event.code === 'Space') {
        event.preventDefault();
        togglePause();
      } else if (event.key === 'ArrowRight') {
        event.preventDefault();
        control('next');
      } else if (event.key === 'ArrowLeft') {
        event.preventDefault();
        control('prev');
      }
    };
    window.addEventListener('keydown', onKeyDown);
    return () => window.removeEventListener('keydown', onKeyDown);
  }, [control, togglePause]);

  const finish = async () => {
    if (finishing) return;
    setFinishing(true);
    control('finish');
    try {
      const result = await api.endSession(session.session_id);
      goSummary(result.recap);
    } catch (error) {
      toast.error(errorText(error));
      setFinishing(false);
    }
  };

  const totalSlides = slideCount || unit.slides;
  const currentSlide = totalSlides ? Math.min(slideIndex + 1, totalSlides) : slideIndex + 1;
  const phase = classroom.phase;
  const phaseTone = paused || phase === 'paused' ? 'warn' : phase === 'ended' ? 'neutral' : 'brand';
  const phaseText = paused || phase === 'paused' ? '已暂停' : phase === 'ended' ? '已结束' : status === 'open' ? '讲解中' : '连接中';

  return (
    <div className="classroom-route">
      <div className="classroom-head">
        <Link to={`/units/${unit.id}/outline`} className="btn btn--ghost btn--sm" aria-label="返回单元大纲">
          <IconArrowLeft size={18} />
        </Link>
        <div style={{ minWidth: 0 }}>
          <div className="classroom-head__title">{unit.title}</div>
          <div className="classroom-head__meta">
            <span>
              第 {currentSlide} / {totalSlides} 页
            </span>
            <span>{formatClock(elapsed)}</span>
            <Badge tone={phaseTone}>{phaseText}</Badge>
            {classroom.comprehension != null && <span>理解度 {Math.round(classroom.comprehension * 100)}%</span>}
          </div>
        </div>
        <span className="spacer" />
        {autoplayBlocked && (
          <Button variant="secondary" size="sm" iconLeft={<IconVolume size={16} />} onClick={classroom.retryAudio}>
            播放声音
          </Button>
        )}
        <button
          type="button"
          className="btn btn--secondary btn--sm drawer-toggle"
          aria-expanded={drawerOpen}
          aria-controls="classroom-sidebar"
          onClick={() => setDrawerOpen((value) => !value)}
        >
          字幕与提问
        </button>
      </div>

      {status !== 'open' && (
        <div className="conn-banner" role="status">
          <span className="dot dot--warn" aria-hidden="true" />
          <span>
            {status === 'connecting'
              ? '正在连接课堂…'
              : `连接断开，正在自动重连（第 ${Math.max(1, retries)} 次）…重连后会从当前进度恢复`}
          </span>
        </div>
      )}

      <div className="classroom-body">
        <div className="stage-wrap">
          <div className="stage">
            {slide ? (
              <SlideView slide={slide} />
            ) : (
              <div className="stage-empty">
                <span className="dot dot--warn" aria-hidden="true" />
                <span>正在准备这一页的课件…</span>
              </div>
            )}
          </div>
          <ControlBar
            paused={paused}
            rate={rate}
            slideIndex={slideIndex}
            slideCount={totalSlides}
            finishing={finishing}
            disabled={status !== 'open'}
            onTogglePause={togglePause}
            onPrev={() => control('prev')}
            onNext={() => control('next')}
            onRate={classroom.setRate}
            onFinish={() => void finish()}
          />
        </div>

        <aside
          id="classroom-sidebar"
          className={'classroom-sidebar' + (drawerOpen ? ' classroom-sidebar--open' : '')}
          aria-label="字幕与提问"
        >
          <TranscriptPanel
            entries={transcript}
            caption={caption}
            connected={status === 'open'}
            onAsk={classroom.ask}
          />
        </aside>
      </div>

      {drawerOpen && (
        <button type="button" className="sidebar-scrim" aria-label="收起字幕面板" onClick={() => setDrawerOpen(false)} />
      )}
    </div>
  );
}
