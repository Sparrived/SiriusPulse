import { QueryClientProvider } from '@tanstack/react-query';
import { useEffect, useState } from 'react';
import { Router } from 'wouter';
import { useHashLocation } from 'wouter/use-hash-location';
import { getToken, onTokenChange } from '@/lib/auth';
import { queryClient } from '@/lib/queries';
import { Door } from '@/rooms/Door';
import { useScene } from '@/state/scene';
import { House } from '@/House';
import { World } from '@/world/World';

export function App() {
  const [token, setTokenState] = useState(getToken);
  const [entered, setEntered] = useState(!!getToken());

  useEffect(
    () =>
      onTokenChange((t) => {
        setTokenState(t);
        if (!t) {
          setEntered(false);
          queryClient.clear();
        }
      }),
    [],
  );

  useEffect(() => {
    if (!entered) useScene.getState().set({ mode: 'door' });
  }, [entered]);

  return (
    <QueryClientProvider client={queryClient}>
      <Router hook={useHashLocation}>
        <World />
        {token && entered ? <House /> : <Door onOpen={() => setEntered(true)} />}
      </Router>
    </QueryClientProvider>
  );
}
