import React, { useState } from 'react';

interface Props {
  title: string;
}

export default function Card({ title }: Props) {
  const [open, setOpen] = useState(false);
  return <div className="card">{title}</div>;
}

export function Button(props: Props) {
  return <button>{props.title}</button>;
}

export const Toggle = () => <span>toggle</span>;
