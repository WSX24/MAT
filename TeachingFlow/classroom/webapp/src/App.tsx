import { Route, Routes } from 'react-router-dom';
import { AppShell } from './components/AppShell';
import { ClassroomPage } from './pages/ClassroomPage';
import { CourseDetailPage } from './pages/CourseDetailPage';
import { CourseLibraryPage } from './pages/CourseLibraryPage';
import { NotFoundPage } from './pages/NotFoundPage';
import { OutlinePage } from './pages/OutlinePage';
import { SummaryPage } from './pages/SummaryPage';

export function App() {
  return (
    <Routes>
      <Route element={<AppShell />}>
        <Route path="/" element={<CourseLibraryPage />} />
        <Route path="/courses/:id" element={<CourseDetailPage />} />
        <Route path="/units/:id/outline" element={<OutlinePage />} />
        <Route path="/learn/:unitId" element={<ClassroomPage />} />
        <Route path="/sessions/:id/summary" element={<SummaryPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
