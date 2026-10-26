import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { Card, EmptyState } from '../components';
import { copy } from '../copy';

export function PlaceholderPage({ title, description, children }: {
  title: string;
  description?: string;
  children?: ReactNode;
}) {
  return (
    <div className="page-stack">
      <header className="page-heading"><h1>{title}</h1><p className="muted">{copy.common.placeholder}</p></header>
      {children}
      <Card><EmptyState description={description} /></Card>
    </div>
  );
}

export function NotFoundPage() {
  return (
    <Card><EmptyState title={copy.service.notFoundTitle} description={copy.service.notFoundDescription}
      action={<Link className="button button--primary" to="/">{copy.service.goHome}</Link>} /></Card>
  );
}
