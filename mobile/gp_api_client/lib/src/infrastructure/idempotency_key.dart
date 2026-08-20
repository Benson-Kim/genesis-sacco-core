/// Idempotency-Key stability and rotation contract.
///
/// Ported from the web client's `idempotencyKeyFor`
/// (`web/packages/api-client/src/index.ts`) rather than re-derived — the
/// contract is a property of the SERVER's idempotency store, so both clients
/// must implement the same rule or the guarantee is not a guarantee.
///
/// The rule: retrying the IDENTICAL logical submission (same canonical body)
/// REUSES the key, so the server replays its stored response instead of
/// re-executing. Changing the content is a NEW logical intent and rotates the
/// key. One key per intent, never per HTTP attempt.
///
/// Scaffold defect D7: the P16 client forwarded an Idempotency-Key only when a
/// caller happened to pass one, and nothing generated or reused keys. That
/// makes FM-G (double-tap creates two effects) unguarded.
library;

import 'package:uuid/uuid.dart';

const Uuid _uuid = Uuid();

/// A fresh key. `fresh` is injected in [IdempotencyKeySlot.keyFor] so the
/// rotation contract has a falsifiable test that does not depend on real
/// randomness.
String newIdempotencyKey() => _uuid.v4();

/// One key slot per logical intent — hold it for the lifetime of the form,
/// sheet, or button that represents that intent, NOT for the lifetime of a
/// request.
class IdempotencyKeySlot {
  String? _key;
  String? _body;

  /// The key for [canonicalBody]: stable across retries of the same content,
  /// rotated the moment the content changes.
  String keyFor(String canonicalBody, {String Function() fresh = newIdempotencyKey}) {
    if (_key == null || _body != canonicalBody) {
      _key = fresh();
      _body = canonicalBody;
    }
    return _key!;
  }

  /// Drop the key after the intent has definitively completed, so the next
  /// use of the same widget is a new logical intent rather than a replay of
  /// the last one.
  void reset() {
    _key = null;
    _body = null;
  }
}
