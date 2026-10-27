import { Link } from 'react-router-dom';

export function NotFoundPage() {
  return (
    <div className="page page--narrow">
      <div className="not-found">
        <div className="not-found__code">404</div>
        <h1 style={{ fontSize: 'var(--text-2xl)' }}>这个页面不存在</h1>
        <p className="muted" style={{ maxWidth: '48ch' }}>
          地址可能抄错了，或者原来的课程已经被删掉。回课程库重新选一门课吧。
        </p>
        <div className="row">
          <Link to="/" className="btn btn--primary">
            返回课程库
          </Link>
        </div>
      </div>
    </div>
  );
}
