/// Guarantor act guards.
///
/// The two that matter are the 409 and the double tap. Everything else here
/// is about not inventing disclosures the server refused to make.
library;

import 'dart:async';

import 'package:flutter_riverpod/flutter_riverpod.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:member_app/src/core/providers.dart';
import 'package:member_app/src/features/guarantees/domain/guarantee_action_controller.dart';
import 'package:member_app/src/features/guarantees/domain/guarantee_port.dart';
import 'package:member_app/src/features/guarantees/domain/guarantee_view.dart';

import '../../support/fakes.dart';

void main() {
  const String id = '7f3a1c22-0000-4000-8000-000000000001';

  ProviderContainer containerWith(FakeGuaranteePort port) {
    final ProviderContainer container = ProviderContainer(
      overrides: <Override>[guaranteePortProvider.overrideWithValue(port)],
    );
    addTearDown(container.dispose);
    return container;
  }

  group('a successful act', () {
    test('renders the server\'s status rather than assuming one', () async {
      // The client never guesses that consent yields `active`. If the server
      // one day returns something else, the screen says what the server said.
      final FakeGuaranteePort port = FakeGuaranteePort();
      final ProviderContainer container = containerWith(port);

      await container
          .read(guaranteeActionControllerProvider.notifier)
          .submit(id, GuaranteeAct.consent, version: 3);

      final GuaranteeActionState state =
          container.read(guaranteeActionControllerProvider);
      expect(state.status, GuaranteeActionStatus.done);
      expect(state.result!.standing, GuaranteeStanding.active);
      expect(state.result!.version, 4);
    });

    test('release reports the withdrawn standing', () async {
      final ProviderContainer container = containerWith(FakeGuaranteePort());

      await container
          .read(guaranteeActionControllerProvider.notifier)
          .submit(id, GuaranteeAct.release, version: 3);

      expect(
        container.read(guaranteeActionControllerProvider).result!.standing,
        GuaranteeStanding.withdrawn,
      );
    });
  });

  group('the version fence', () {
    test('a 409 is terminal, and says so without offering a retry', () async {
      // There is no member facing GET for a guarantee (#41), so the app
      // cannot refetch a fresh version. A retry would resend the same dead
      // value and fail identically. The state is `stale`, not `failed`, so
      // the sheet can render it as a different thing from an error.
      final FakeGuaranteePort port = FakeGuaranteePort(stale: true);
      final ProviderContainer container = containerWith(port);

      await container
          .read(guaranteeActionControllerProvider.notifier)
          .submit(id, GuaranteeAct.consent, version: 3);

      final GuaranteeActionState state =
          container.read(guaranteeActionControllerProvider);
      expect(state.status, GuaranteeActionStatus.stale);
      expect(state.message, contains('changed while it was open'));
      expect(state.correlationId, 'c-409');
    });
  });

  group('the single refusal (gate 1.6)', () {
    test('a 403 does not distinguish why', () async {
      // The server answers one 403 for a dead link, an exited member,
      // somebody else's guarantee and an already consented one. The message
      // must not narrow that down, because narrowing it would disclose
      // something the server deliberately withheld.
      final ProviderContainer container =
          containerWith(FakeGuaranteePort(forbidden: true));

      await container
          .read(guaranteeActionControllerProvider.notifier)
          .submit(id, GuaranteeAct.consent, version: 3);

      final String message =
          container.read(guaranteeActionControllerProvider).message!;
      for (final String forbidden in <String>[
        'exited',
        'expired',
        'someone else',
        'not yours',
        'already',
        'revoked',
      ]) {
        expect(message.toLowerCase(), isNot(contains(forbidden)),
            reason: 'the server returns ONE refusal for every wrong-actor '
                'shape; naming one of them invents a disclosure');
      }
    });
  });

  group('double submit (FM-G)', () {
    test('a second submit while one is in flight is dropped', () async {
      final Completer<void> gate = Completer<void>();
      final FakeGuaranteePort port = FakeGuaranteePort(gate: gate);
      final ProviderContainer container = containerWith(port);
      final GuaranteeActionController controller =
          container.read(guaranteeActionControllerProvider.notifier);

      final Future<void> first =
          controller.submit(id, GuaranteeAct.consent, version: 3);
      await controller.submit(id, GuaranteeAct.consent, version: 3);

      expect(port.calls, 1, reason: 'the busy check is the guard; remove it '
          'and a double tap consents twice');
      gate.complete();
      await first;
      expect(port.calls, 1);
    });
  });
}
