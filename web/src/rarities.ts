/** Reference tier IDs and the separately preserved legacy Epic value. */
export const rarities = [
  { value: 'common', label: 'Common', id: 1 },
  { value: 'rare', label: 'Rare', id: 2 },
  { value: 'special-edition', label: 'Special Edition', id: 3 },
  { value: 'legendary', label: 'Legendary', id: 4 },
  { value: 'mythic-edition', label: 'Mythic Edition', id: 5 },
  { value: 'valentine-edition', label: 'Valentine Edition', id: 6 },
  { value: 'summer-edition', label: 'Summer Edition', id: 7 },
  { value: 'rainy-edition', label: 'Rainy Edition', id: 8 },
  { value: 'halloween-edition', label: 'Halloween Edition', id: 9 },
  { value: 'christmas-edition', label: 'Christmas Edition', id: 10 },
  { value: 'winter-edition', label: 'Winter Edition', id: 11 },
  { value: 'new-year-edition', label: 'New Year Edition', id: 12 },
  { value: 'festival-edition', label: 'Festival Edition', id: 13 },
  { value: 'amv-edition', label: 'AMV Edition', id: 14 },
  { value: 'event-edition', label: 'Event Edition', id: 15 },
  { value: 'celestial-edition', label: 'Celestial Edition', id: 16 },
  { value: 'luxury-edition', label: 'Luxury Edition', id: 17 },
  { value: 'limited-edition', label: 'Limited Edition', id: 18 },
  { value: 'epic', label: 'Epic (legacy)', id: null },
] as const;

export type Rarity = typeof rarities[number]['value'];
export const rarityLabel = (value: Rarity): string =>
  rarities.find(rarity => rarity.value === value)?.label ?? value;
