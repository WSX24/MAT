import { useState } from 'react';
import { Link } from 'react-router-dom';
import { api } from '../api/client';
import type { ImportResult } from '../api/types';
import { useAsync } from '../hooks/useAsync';
import { errorText, formatDate, sourceLabel } from '../lib/format';
import { useApp } from '../state/AppState';
import { Button } from '../components/Button';
import { Empty } from '../components/Empty';
import { IconChevronRight, IconRefresh, IconSpark, IconUpload } from '../components/icons';
import { ProgressBar, ProgressRing } from '../components/Progress';
import { SkeletonCards } from '../components/Skeleton';
import { useToast } from '../components/Toast';
import { ImportCourseModal } from '../features/course/ImportCourseModal';
import { ProduceModal } from '../features/course/ProduceModal';

export function CourseLibraryPage() {
  const { activeProfile, profilesStatus, profilesError, reloadProfiles } = useApp();
  const toast = useToast();
  const courses = useAsync(() => api.listCourses(activeProfile?.id), [activeProfile?.id]);
  const [scanning, setScanning] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [produceOpen, setProduceOpen] = useState(false);

  const scan = async () => {
    setScanning(true);
    try {
      const result = await api.scanCourses();
      toast.ok(result.added || result.updated
        ? `扫描完成：新增 ${result.added} 门，更新 ${result.updated} 门，共 ${result.total} 门`
        : `扫描完成：没有发现新课程（共 ${result.total} 门）`);
      courses.reload();
    } catch (error) {
      toast.error(errorText(error));
    } finally {
      setScanning(false);
    }
  };

  const handleImported = (_result: ImportResult) => {
    courses.reload();
  };

  const produce = () => {
    const url = import.meta.env.VITE_PRODUCE_URL;
    if (url) {
      window.open(url, '_blank', 'noreferrer');
      return;
    }
    setProduceOpen(true);
  };

  const list = courses.data ?? [];
  const isEmpty = courses.status === 'ready' && list.length === 0;

  return (
    <div className="page">
      <div className="page-head">
        <div className="page-head__main">
          <div className="page-head__eyebrow">课程库</div>
          <h1 className="page-head__title">我的课堂</h1>
          <p className="page-head__sub">
            选一门课，进到章与单元，课堂上会逐页讲解、逐句出声；不懂就随时打断提问，进度按档案各自记录。
          </p>
        </div>
        <div className="page-head__actions">
          <Button variant="accent" iconLeft={<IconSpark size={18} />} onClick={produce}>
            去出片
          </Button>
          <Button variant="primary" iconLeft={<IconUpload size={18} />} onClick={() => setImportOpen(true)}>
            导入课件
          </Button>
          <Button
            variant="ghost"
            iconLeft={<IconRefresh size={18} />}
            loading={scanning}
            onClick={() => void scan()}
            aria-label="重新扫描 runs 目录里的课程"
          >
            重新扫描
          </Button>
        </div>
      </div>

      {profilesStatus === 'error' && (
        <div className="banner banner--warn" role="alert" style={{ marginBottom: 'var(--space-5)' }}>
          <span style={{ flex: 1 }}>档案加载失败：{profilesError}</span>
          <Button size="sm" variant="secondary" onClick={() => void reloadProfiles()}>
            重试
          </Button>
        </div>
      )}

      {courses.status === 'loading' && <SkeletonCards count={6} />}

      {courses.status === 'error' && (
        <Empty
          art="error"
          error
          title="课程库没能加载出来"
          description={courses.error || '请确认后端服务在运行，然后重试。'}
          actions={
            <>
              <Button variant="primary" onClick={courses.reload}>
                重试
              </Button>
              <Button variant="secondary" onClick={() => void reloadProfiles()}>
                重新加载档案
              </Button>
            </>
          }
        />
      )}

      {isEmpty && (
        <Empty
          art="courses"
          title="还没有课程"
          description="两种方式把课放进来：让 MAT 出一份片（一句话需求 → pptx，出片后自动备课），或者直接导入一份现成的 .pptx。放进来后点「重新扫描」或「导入课件」就能看到。"
          actions={
            <>
              <Button variant="primary" iconLeft={<IconSpark size={18} />} onClick={produce}>
                去出片
              </Button>
              <Button variant="secondary" iconLeft={<IconUpload size={18} />} onClick={() => setImportOpen(true)}>
                导入课件
              </Button>
              <Button variant="ghost" iconLeft={<IconRefresh size={18} />} loading={scanning} onClick={() => void scan()}>
                重新扫描
              </Button>
            </>
          }
        />
      )}

      {courses.status === 'ready' && list.length > 0 && (
        <>
          <div className="section-title">
            <h2>全部课程</h2>
            <span className="muted" style={{ fontSize: 'var(--text-sm)' }}>
              共 {list.length} 门 · {list.reduce((sum, course) => sum + course.unit_count, 0)} 个单元
            </span>
          </div>
          <div className="library-grid">
            {list.map((course) => {
              const percent = Math.round(course.progress.percent || 0);
              const progressText = percent <= 0 ? '还没开始' : percent >= 100 ? '已学完' : `已学 ${percent}%`;
              return (
                <Link key={course.id} to={`/courses/${course.id}`} className="card card--interactive course-card">
                  <div className="course-card__head">
                    <div className="course-card__titles">
                      <span className="course-card__subject">
                        {course.subject || '未分类'} · {sourceLabel(course.source)}
                      </span>
                      <div className="course-card__title">{course.title}</div>
                    </div>
                    <ProgressRing value={percent} size={62} label={`${course.title} 学习进度`} />
                  </div>
                  <div className="course-card__meta">
                    <span>
                      <strong>{course.unit_count}</strong> 个单元
                    </span>
                    <span>
                      <strong>{course.unit_ready_count}</strong> 已备课
                    </span>
                    <span>
                      <strong>{course.progress.completed}</strong> 已完成
                    </span>
                    <span>更新 {formatDate(course.updated_at)}</span>
                  </div>
                  <div className="course-card__foot">
                    <div className="course-card__progress">
                      <span>{progressText}</span>
                      <ProgressBar value={percent} size="sm" label={`${course.title} 学习进度`} />
                    </div>
                    <span className="course-card__cta">
                      进入课程 <IconChevronRight size={16} />
                    </span>
                  </div>
                </Link>
              );
            })}
          </div>
        </>
      )}

      <ImportCourseModal open={importOpen} onClose={() => setImportOpen(false)} onImported={handleImported} />
      <ProduceModal open={produceOpen} onClose={() => setProduceOpen(false)} />
    </div>
  );
}
