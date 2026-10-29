import type { ReactNode } from 'react';
import { Link } from 'react-router';
import { EmptyState, Icon, PageHeader } from '../components';
import { copy } from '../copy';
import './Service.css';

export function PlaceholderPage({ title, description, children }: {
  title: string;
  description?: string;
  children?: ReactNode;
}) {
  return (
    <div className="page-stack">
      <PageHeader title={title} description={copy.common.placeholder} />
      {children}
      <section className="panel service-panel"><EmptyState description={description} /></section>
    </div>
  );
}

export function NotFoundPage() {
  return (
    <div className="service-center">
      <section className="panel service-panel">
        <EmptyState level={1} icon="pilot" title={copy.service.notFoundTitle} description={copy.service.notFoundDescription}
          action={<Link className="button button--primary" to="/"><Icon name="home" />{copy.service.goHome}</Link>} />
      </section>
    </div>
  );
}
