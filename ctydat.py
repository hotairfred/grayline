"""
ctydat.py — DXCC Country File (cty.dat) Parser

Parses AD1C's cty.dat file and provides callsign-to-DXCC-entity lookup
using the standard longest-prefix-match algorithm.

Usage:
    lookup = CtyDat('cty.dat')
    entity = lookup.lookup('J51ABC')
    # -> {'entity': 'Guinea-Bissau', 'prefix': 'J5', 'cq_zone': 35,
    #     'itu_zone': 46, 'continent': 'AF', 'lat': 12.0, 'lon': 15.0,
    #     'utc_offset': 0.0}
"""

import re
from dataclasses import dataclass
from typing import Optional


@dataclass
class DXCCEntity:
    """A DXCC entity record from cty.dat."""
    entity: str
    cq_zone: int
    itu_zone: int
    continent: str
    lat: float
    lon: float
    utc_offset: float
    prefix: str  # primary prefix (e.g., 'DL' for Germany)


class CtyDat:
    """DXCC prefix lookup from cty.dat file."""

    def __init__(self, filename: str = 'cty.dat'):
        # prefix string -> DXCCEntity
        self._prefixes = {}
        # exact callsign -> DXCCEntity (for =CALL entries)
        self._exact = {}
        # primary prefix -> DXCCEntity (for quick lookup by prefix)
        self._entities = {}
        self._load(filename)

    def _load(self, filename: str):
        """Parse cty.dat file."""
        with open(filename, 'r') as f:
            content = f.read()

        # Split into records — each record starts with a non-space line
        # and continues with space-prefixed lines until a semicolon
        records = re.split(r'\n(?=\S)', content.strip())

        for record in records:
            lines = record.strip().split('\n')
            if not lines:
                continue

            # Parse header line
            # Format: Entity: CQz: ITUz: Cont: Lat: Lon: UTC: Prefix:
            header = lines[0]
            parts = [p.strip() for p in header.split(':')]
            if len(parts) < 8:
                continue

            try:
                entity = DXCCEntity(
                    entity=parts[0].strip(),
                    cq_zone=int(parts[1]),
                    itu_zone=int(parts[2]),
                    continent=parts[3].strip(),
                    lat=float(parts[4]),
                    lon=-float(parts[5]),  # cty.dat uses west-positive
                    utc_offset=-float(parts[6]),  # cty.dat uses west-positive
                    prefix=parts[7].strip(),
                )
            except (ValueError, IndexError):
                continue

            self._entities[entity.prefix.upper()] = entity

            # Add primary prefix
            self._prefixes[entity.prefix.upper()] = entity

            # Parse alias prefixes from continuation lines
            alias_text = ''.join(lines[1:])
            # Remove trailing semicolon and split by comma
            alias_text = alias_text.rstrip(';').strip()
            if not alias_text:
                continue

            for alias in alias_text.split(','):
                alias = alias.strip()
                if not alias:
                    continue

                # Strip zone overrides: (CQz) [ITUz] {continent}
                clean = re.sub(r'\(\d+\)|\[\d+\]|\{[A-Z]+\}', '', alias).strip()

                if clean.startswith('='):
                    # Exact callsign match
                    call = clean[1:].upper()
                    self._exact[call] = entity
                else:
                    self._prefixes[clean.upper()] = entity

    def lookup(self, callsign: str) -> Optional[DXCCEntity]:
        """Look up a callsign and return its DXCC entity.

        Uses longest-prefix-match algorithm:
        1. Check exact callsign matches first
        2. Handle portable indicators (/VP9, VP9/)
        3. Try progressively shorter prefixes
        """
        call = callsign.upper().strip()

        # Check exact match first — preserves cty.dat's explicit overrides
        # (both the stateside =KG4MLB exceptions and any oddball =KG44WW
        # Guantanamo entries) above the general rule below.
        if call in self._exact:
            return self._exact[call]

        # Guantanamo Bay deterministic DXCC rule. cty.dat encodes this as an
        # enumeration (KG4 prefix -> Guantanamo, then every stateside KG4 call
        # listed as a =CALL exception), so any un-enumerated KG4 call falls
        # through the greedy KG4 prefix and mis-resolves to Guantanamo. The
        # real rule is computable: KG4 + exactly two letters = Guantanamo;
        # KG4 + anything else = US fourth call area. Apply it directly so the
        # result no longer depends on how current cty.dat happens to be.
        kg4 = self._kg4_rule(call)
        if kg4 is not None:
            return kg4

        # Handle portable indicators
        if '/' in call:
            parts = call.split('/')
            if len(parts) == 2:
                # Could be W1ABC/VP9 or VP9/W1ABC
                # The shorter part is usually the prefix indicator
                if len(parts[0]) < len(parts[1]):
                    # VP9/W1ABC — first part is the location
                    prefix_part = parts[0]
                else:
                    # W1ABC/VP9 — second part is the location
                    prefix_part = parts[1]

                # But ignore single-char suffixes like /P /M /QRP
                if len(prefix_part) <= 1 or prefix_part in ('QRP', 'MM', 'AM', 'P', 'M', 'A'):
                    call = parts[0]  # Use the main call
                else:
                    # Try the prefix part as a location override
                    result = self._prefix_match(prefix_part)
                    if result:
                        return result
                    call = parts[0]  # Fall back to main call

        return self._prefix_match(call)

    def _kg4_rule(self, call: str) -> Optional[DXCCEntity]:
        """Apply the Guantanamo Bay 2-letter rule to a bare KG4 callsign.

        KG4 + exactly two letters -> Guantanamo Bay; KG4 + anything else
        (one letter, three or more chars, or a digit) -> US fourth call area.
        Returns None for non-KG4 or slashed calls, so portable forms
        (W1AW/KG4, KG4XX/P) keep their existing exact/prefix handling.
        """
        if '/' in call or not call.startswith('KG4'):
            return None
        suffix = call[3:]
        if len(suffix) == 2 and suffix.isalpha():
            return self._prefixes.get('KG4')   # Guantanamo Bay (DXCC 105)
        return self._prefixes.get('K')         # United States (DXCC 291)

    def _prefix_match(self, call: str) -> Optional[DXCCEntity]:
        """Find the longest prefix match for a callsign."""
        # Try progressively shorter prefixes
        for length in range(len(call), 0, -1):
            prefix = call[:length]
            if prefix in self._prefixes:
                return self._prefixes[prefix]
        return None

    def get_entity_by_prefix(self, prefix: str) -> Optional[DXCCEntity]:
        """Look up entity by its primary DXCC prefix."""
        return self._entities.get(prefix.upper())

    @property
    def entity_count(self) -> int:
        """Number of DXCC entities loaded."""
        return len(self._entities)

    @property
    def prefix_count(self) -> int:
        """Total number of prefixes (including aliases)."""
        return len(self._prefixes) + len(self._exact)


if __name__ == '__main__':
    import sys

    cty = CtyDat('cty.dat')
    print(f"Loaded {cty.entity_count} entities, {cty.prefix_count} prefixes")
    print()

    # Test lookups
    test_calls = [
        'W1ABC', 'DL3ABC', 'J51A', '3Y0K', 'KH6ABC',
        'UA0ABC', 'VP2MAA', 'TT1GD', 'VK3FRO', 'P4/WE9V',
        'W1ABC/VP9', 'EA8/DF4UE', 'ZS8M', 'FY5KE',
    ]

    if len(sys.argv) > 1:
        test_calls = sys.argv[1:]

    for call in test_calls:
        entity = cty.lookup(call)
        if entity:
            print(f"  {call:<15s} -> {entity.prefix:<6s} {entity.entity} "
                  f"({entity.continent}, CQ{entity.cq_zone})")
        else:
            print(f"  {call:<15s} -> NOT FOUND")
