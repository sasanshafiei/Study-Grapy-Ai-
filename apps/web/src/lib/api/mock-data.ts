/**
 * Sample data for mock mode. Everything here is invented for the demo course
 * (General Chemistry I). Nothing in this file comes from a real class.
 */

import type {
  Citation,
  Course,
  Difficulty,
  DocumentItem,
  ExamFlag,
  Job,
  Origin,
  QuestionType,
  Source,
  Topic,
  TopicMastery,
  User,
} from "./types";

export const COURSE: Course = {
  id: "course-chem101",
  code: "CHEM 101",
  name: "General Chemistry I",
  professorName: "Dr. Demo Professor",
  joinCode: "CHEM-7Q4X",
  studentCount: 42,
};

export const TOPICS: Topic[] = [
  { id: "t-matter", name: "Matter and Measurement" },
  { id: "t-atomic", name: "Atomic Structure" },
  { id: "t-bonding", name: "Chemical Bonding" },
  { id: "t-stoich", name: "Stoichiometry" },
  { id: "t-solutions", name: "Solutions" },
  { id: "t-thermo", name: "Thermochemistry" },
  { id: "t-equil", name: "Equilibrium" },
  { id: "t-acids", name: "Acids and Bases" },
];

export function topicName(id: string): string {
  return TOPICS.find((t) => t.id === id)?.name ?? "General";
}

// --- Time helpers (called at request time, never at module load) -------------

export function daysAgo(days: number, hour = 12): string {
  const d = new Date();
  d.setDate(d.getDate() - days);
  d.setHours(hour, 0, 0, 0);
  return d.toISOString();
}

export function dayKey(days: number): string {
  return daysAgo(days).slice(0, 10);
}

// --- Sources that answers and questions cite ---------------------------------

interface SourceRef {
  origin: Origin;
  title: string;
  locator: string;
  url: string | null;
  snippet: string;
}

const L1 = "Lecture 01 - Matter and Measurement.pdf";
const L2 = "Lecture 02 - Atomic Structure.pdf";
const L3 = "Lecture 03 - Chemical Bonding.pdf";
const L4 = "Lecture 04 - Stoichiometry.pdf";
const L5 = "Lecture 05 - Solutions.pdf";
const L6 = "Lecture 06 - Thermochemistry.pdf";
const L7 = "Lecture 07 - Chemical Equilibrium.pdf";
const L8 = "Lecture 08 - Acids and Bases.pdf";

const cls = (title: string, slide: number, snippet: string): SourceRef => ({
  origin: "class",
  title,
  locator: `Slide ${slide}`,
  url: null,
  snippet,
});

const openstax = (
  section: string,
  name: string,
  slug: string,
  snippet: string,
): SourceRef => ({
  origin: "external",
  title: `OpenStax Chemistry 2e, ${section} ${name}`,
  locator: `Section ${section}`,
  url: `https://openstax.org/books/chemistry-2e/pages/${slug}`,
  snippet,
});

const wiki = (
  name: string,
  slug: string,
  snippet: string,
  origin: Origin = "external",
): SourceRef => ({
  origin,
  title: `${name}, Wikipedia`,
  locator: "Article",
  url: `https://en.wikipedia.org/wiki/${slug}`,
  snippet,
});

export const SOURCES_REF = {
  l1SigFigs: cls(L1, 8, "Leading zeros are never significant. Trailing zeros after a decimal point are."),
  l1Mixtures: cls(L1, 12, "A homogeneous mixture has the same composition throughout."),
  l2Isotopes: cls(L2, 5, "Isotopes have the same number of protons and different numbers of neutrons."),
  l2Valence: cls(L2, 9, "For main-group elements, the number of valence electrons matches the group number."),
  l2Radius: cls(L2, 14, "Across a period, effective nuclear charge rises and atomic radius falls."),
  l3Ionic: cls(L3, 4, "Ionic bonds form when a metal transfers electrons to a nonmetal."),
  l3Vsepr: cls(L3, 11, "Electron groups around a central atom arrange themselves to minimise repulsion."),
  l3Polarity: cls(L3, 13, "A molecule is polar when its bond dipoles do not cancel."),
  l4Ratios: cls(L4, 6, "Coefficients in a balanced equation are mole ratios."),
  l4Moles: cls(L4, 9, "Moles = mass / molar mass."),
  l4Limiting: cls(L4, 12, "The limiting reactant is used up first and sets how much product can form."),
  l4Method: cls(L4, 15, "Divide the moles of each reactant by its coefficient. The smallest value marks the limiting reactant."),
  l4Yield: cls(L4, 18, "Percent yield = actual yield / theoretical yield x 100%."),
  l5Molarity: cls(L5, 4, "Molarity (M) = moles of solute / litres of solution."),
  l5Dilution: cls(L5, 9, "Dilution keeps the moles of solute constant, so M1V1 = M2V2."),
  l6Sign: cls(L6, 5, "Negative ΔH: exothermic, heat leaves the system. Positive ΔH: endothermic."),
  l6Calorimetry: cls(L6, 11, "q = m c ΔT. For water, c = 4.18 J/g·°C."),
  l6Hess: cls(L6, 16, "The enthalpy change of an overall reaction equals the sum over its steps."),
  l7Dynamic: cls(L7, 3, "At equilibrium the forward and reverse rates are equal. Both reactions continue."),
  l7Kc: cls(L7, 4, "Kc is the ratio of product to reactant concentrations, each raised to its coefficient."),
  l7Principle: cls(L7, 7, "A disturbed equilibrium shifts in the direction that partly offsets the disturbance."),
  l7Pressure: cls(L7, 10, "Decreasing the volume favours the side with fewer moles of gas."),
  l7Temperature: cls(L7, 12, "Only temperature changes K. Heating an exothermic reaction lowers K."),
  l7Catalyst: cls(L7, 14, "A catalyst speeds up both directions equally. The equilibrium position does not move."),
  l8Bronsted: cls(L8, 3, "A Brønsted-Lowry acid donates a proton. A base accepts one."),
  l8Ph: cls(L8, 5, "pH = -log[H3O+]."),
  l8Sum: cls(L8, 7, "At 25 °C, pH + pOH = 14.00."),
  l8Titration: cls(L8, 13, "At the equivalence point, moles of acid equal moles of base."),

  osMolarity: openstax("3.3", "Molarity", "3-3-molarity", "Adding solvent lowers the concentration while the amount of solute stays the same."),
  osYields: openstax("4.4", "Reaction Yields", "4-4-reaction-yields", "Work out how much product each reactant could make. The one that makes less is limiting."),
  osCalorimetry: openstax("5.2", "Calorimetry", "5-2-calorimetry", "Heat gained or lost is found from mass, specific heat and temperature change."),
  osEnthalpy: openstax("5.3", "Enthalpy", "5-3-enthalpy", "Enthalpy is a state function, so the path between reactants and products does not matter."),
  osPolarity: openstax("7.6", "Molecular Structure and Polarity", "7-6-molecular-structure-and-polarity", "Linear CO2 has two equal bond dipoles pointing in opposite directions, so they cancel."),
  osShifting: openstax("13.3", "Shifting Equilibria: Le Châtelier's Principle", "13-3-shifting-equilibria-le-chateliers-principle", "Compressing a gas-phase equilibrium favours the side with fewer gas molecules."),
  osPh: openstax("14.2", "pH and pOH", "14-2-ph-and-poh", "For water at 25 °C, pH and pOH add up to 14.00."),

  wikiLimiting: wiki("Limiting reagent", "Limiting_reagent", "Reactants left over once the limiting reagent is consumed are in excess."),
  wikiLeChatelier: wiki("Le Chatelier's principle", "Le_Chatelier%27s_principle", "Among changes in concentration, pressure and temperature, only temperature changes the equilibrium constant."),
  wikiHess: wiki("Hess's law", "Hess%27s_law", "The total enthalpy change is independent of the route taken."),
  webHaber: wiki("Haber process", "Haber_process", "Industrial plants run at high pressure and roughly 400 to 500 °C over an iron catalyst.", "web"),
} satisfies Record<string, SourceRef>;

export type SourceKey = keyof typeof SOURCES_REF;

export function cite(key: SourceKey, marker = 1): Citation {
  const s: SourceRef = SOURCES_REF[key];
  return { marker, chunkId: `chunk-${key}`, ...s };
}

// --- Question bank ----------------------------------------------------------

export interface BankQuestion {
  id: string;
  topicId: string;
  type: QuestionType;
  difficulty: Difficulty;
  stem: string;
  choices: string[];
  answer: string;
  hint: string;
  explanation: string;
  rubric: string;
  source: SourceKey;
  /** Seeded class statistics, used by the professor's item analysis. */
  correctRate: number;
  discrimination: number;
  flags?: ExamFlag[];
}

const mcq = (
  id: string,
  topicId: string,
  difficulty: Difficulty,
  stem: string,
  choices: string[],
  answer: string,
  hint: string,
  explanation: string,
  source: SourceKey,
  correctRate: number,
  discrimination: number,
  flags?: ExamFlag[],
): BankQuestion => ({
  id,
  topicId,
  type: "mcq",
  difficulty,
  stem,
  choices,
  answer,
  hint,
  explanation,
  rubric: "",
  source,
  correctRate,
  discrimination,
  flags,
});

const tf = (
  id: string,
  topicId: string,
  stem: string,
  answer: "True" | "False",
  explanation: string,
  source: SourceKey,
  correctRate: number,
  discrimination: number,
): BankQuestion => ({
  id,
  topicId,
  type: "true_false",
  difficulty: "easy",
  stem,
  choices: ["True", "False"],
  answer,
  hint: "Read the statement against the definition on the cited slide.",
  explanation,
  rubric: "",
  source,
  correctRate,
  discrimination,
});

const short = (
  id: string,
  topicId: string,
  difficulty: Difficulty,
  stem: string,
  answer: string,
  rubric: string,
  source: SourceKey,
  flags?: ExamFlag[],
): BankQuestion => ({
  id,
  topicId,
  type: "short",
  difficulty,
  stem,
  choices: [],
  answer,
  hint: "",
  explanation: answer,
  rubric,
  source,
  correctRate: 0.5,
  discrimination: 0.4,
  flags,
});

export const BANK: BankQuestion[] = [
  // Matter and Measurement
  mcq("q-sigfig", "t-matter", "easy",
    "How many significant figures are in 0.00450?",
    ["2", "3", "5", "6"], "3",
    "Leading zeros only place the decimal point.",
    "The leading zeros are not significant. The digits 4 and 5 are, and so is the trailing zero after the decimal point, which gives three.",
    "l1SigFigs", 0.84, 0.31),
  mcq("q-air", "t-matter", "easy",
    "Clean, dry air is best classified as which of these?",
    ["An element", "A compound", "A homogeneous mixture", "A heterogeneous mixture"],
    "A homogeneous mixture",
    "Is the composition the same in every sample you could take?",
    "Air is several gases mixed uniformly, mainly nitrogen and oxygen. Uniform composition without chemical bonding between the components makes it a homogeneous mixture.",
    "l1Mixtures", 0.88, 0.27),

  // Atomic Structure
  mcq("q-radius", "t-atomic", "easy",
    "Moving from left to right across a period, atomic radius generally does what?",
    ["Increases", "Decreases", "Stays the same", "Increases, then decreases"],
    "Decreases",
    "Think about how strongly the nucleus pulls on the outer electrons.",
    "Each step adds a proton while the outer electrons stay in the same shell. Effective nuclear charge rises, pulling the electrons closer, so the radius falls.",
    "l2Radius", 0.71, 0.42),
  mcq("q-valence", "t-atomic", "medium",
    "How many valence electrons does a neutral phosphorus atom (Z = 15) have?",
    ["3", "5", "8", "15"], "5",
    "Phosphorus is in group 15. Write its outer-shell configuration.",
    "Phosphorus is 1s² 2s² 2p⁶ 3s² 3p³. The outer shell holds 2 + 3 = 5 electrons.",
    "l2Valence", 0.69, 0.45),

  // Chemical Bonding
  mcq("q-nacl", "t-bonding", "easy",
    "The bond in sodium chloride, NaCl, is best described as which type?",
    ["Ionic", "Nonpolar covalent", "Polar covalent", "Metallic"], "Ionic",
    "One element is a metal and the other a nonmetal.",
    "Sodium transfers an electron to chlorine, giving Na⁺ and Cl⁻ held together by electrostatic attraction. That is an ionic bond.",
    "l3Ionic", 0.9, 0.22),
  mcq("q-co2-shape", "t-bonding", "medium",
    "What is the molecular geometry of CO₂?",
    ["Bent", "Linear", "Trigonal planar", "Tetrahedral"], "Linear",
    "Count the electron groups around carbon. Are any of them lone pairs?",
    "Carbon has two electron groups (two double bonds) and no lone pairs. Two groups sit 180° apart, so the molecule is linear.",
    "l3Vsepr", 0.66, 0.48),
  mcq("q-polar", "t-bonding", "medium",
    "Which of these molecules is polar?",
    ["CO₂", "CCl₄", "NH₃", "BF₃"], "NH₃",
    "Symmetric shapes let bond dipoles cancel. Which shape here is not symmetric?",
    "NH₃ is trigonal pyramidal with a lone pair on nitrogen, so its bond dipoles do not cancel. The other three are symmetric and nonpolar.",
    "l3Polarity", 0.58, 0.51),

  // Stoichiometry
  mcq("q-moles-water", "t-stoich", "easy",
    "How many moles are in 36.0 g of water? The molar mass of water is 18.0 g/mol.",
    ["0.500 mol", "1.00 mol", "2.00 mol", "648 mol"], "2.00 mol",
    "Moles = mass / molar mass.",
    "36.0 g / 18.0 g/mol = 2.00 mol.",
    "l4Moles", 0.86, 0.3),
  mcq("q-co2-ratio", "t-stoich", "medium",
    "A reaction produces 2 mol of CO₂ for every 1 mol of fuel burned. How many moles of CO₂ form from 3.5 mol of fuel?",
    ["1.75 mol", "3.5 mol", "5.25 mol", "7.0 mol"], "7.0 mol",
    "Use the mole ratio from the balanced equation as a conversion factor.",
    "3.5 mol fuel x (2 mol CO₂ / 1 mol fuel) = 7.0 mol CO₂.",
    "l4Ratios", 0.74, 0.44),
  mcq("q-limiting", "t-stoich", "medium",
    "For 2 H₂ + O₂ → 2 H₂O, 4.0 mol of H₂ is mixed with 3.0 mol of O₂. Which reactant is limiting?",
    ["H₂", "O₂", "H₂O", "Neither, they are in exact proportion"], "H₂",
    "Divide the moles of each reactant by its coefficient and compare.",
    "H₂: 4.0 / 2 = 2.0. O₂: 3.0 / 1 = 3.0. Hydrogen gives the smaller value, so it runs out first. Only 2.0 mol of O₂ is needed, leaving 1.0 mol in excess.",
    "l4Method", 0.52, 0.56),
  mcq("q-yield", "t-stoich", "hard",
    "A reaction has a theoretical yield of 12.0 g and produces 9.0 g of product. What is the percent yield?",
    ["3.0%", "25%", "75%", "133%"], "75%",
    "Actual over theoretical, times 100.",
    "9.0 g / 12.0 g x 100 = 75%.",
    "l4Yield", 0.63, 0.47, ["possible_duplicate"]),

  // Solutions
  mcq("q-molarity", "t-solutions", "easy",
    "What is the molarity of a solution that contains 0.50 mol of NaCl in 2.0 L of solution?",
    ["0.25 M", "1.0 M", "2.5 M", "4.0 M"], "0.25 M",
    "Molarity is moles of solute per litre of solution.",
    "0.50 mol / 2.0 L = 0.25 M.",
    "l5Molarity", 0.81, 0.33),
  mcq("q-dilution", "t-solutions", "medium",
    "What volume of 6.0 M HCl is needed to prepare 300 mL of 1.0 M HCl?",
    ["18 mL", "50 mL", "180 mL", "1800 mL"], "50 mL",
    "Moles of solute do not change on dilution: M₁V₁ = M₂V₂.",
    "V₁ = (1.0 M x 300 mL) / 6.0 M = 50 mL. Add water to bring the total to 300 mL.",
    "l5Dilution", 0.57, 0.52),

  // Thermochemistry
  mcq("q-exo", "t-thermo", "easy",
    "A reaction has ΔH = -890 kJ/mol. What does the sign tell you?",
    [
      "The reaction is exothermic and releases heat",
      "The reaction is endothermic and absorbs heat",
      "No heat is exchanged",
      "The reaction needs a catalyst",
    ],
    "The reaction is exothermic and releases heat",
    "A negative ΔH means the products hold less enthalpy than the reactants.",
    "Negative ΔH means enthalpy falls from reactants to products. The difference leaves the system as heat, so the reaction is exothermic.",
    "l6Sign", 0.79, 0.36),
  mcq("q-calorimetry", "t-thermo", "medium",
    "How much heat is needed to warm 50.0 g of water by 10.0 °C? The specific heat of water is 4.18 J/g·°C.",
    ["209 J", "418 J", "2090 J", "20 900 J"], "2090 J",
    "Use q = m c ΔT.",
    "q = 50.0 g x 4.18 J/g·°C x 10.0 °C = 2090 J.",
    "l6Calorimetry", 0.49, 0.54),

  // Equilibrium
  mcq("q-catalyst", "t-equil", "easy",
    "What happens when a catalyst is added to a system already at equilibrium?",
    [
      "The position shifts toward products",
      "The position shifts toward reactants",
      "The equilibrium constant increases",
      "The position does not change",
    ],
    "The position does not change",
    "A catalyst lowers the activation energy in both directions.",
    "A catalyst speeds up the forward and reverse reactions by the same factor. Equilibrium is reached sooner, but its position and K stay the same.",
    "l7Catalyst", 0.55, 0.49),
  mcq("q-pressure", "t-equil", "medium",
    "For N₂(g) + 3 H₂(g) ⇌ 2 NH₃(g), the volume is reduced so the pressure rises. Which way does the equilibrium shift?",
    [
      "Toward NH₃, the side with fewer moles of gas",
      "Toward N₂ and H₂, the side with more moles of gas",
      "It does not shift",
      "It shifts both ways equally",
    ],
    "Toward NH₃, the side with fewer moles of gas",
    "Count the moles of gas on each side of the equation.",
    "There are 4 mol of gas on the left and 2 mol on the right. Higher pressure favours the side with fewer gas molecules, so the system makes more NH₃.",
    "l7Pressure", 0.41, 0.58),
  mcq("q-temperature", "t-equil", "medium",
    "An exothermic reaction is at equilibrium. What happens when the temperature is raised?",
    [
      "It shifts toward products and K increases",
      "It shifts toward reactants and K decreases",
      "The position does not change",
      "It shifts toward products and K stays the same",
    ],
    "It shifts toward reactants and K decreases",
    "Treat heat as a product of an exothermic reaction.",
    "Heat is released in the forward direction. Adding heat pushes the system back toward reactants, and because temperature changed, K itself becomes smaller.",
    "l7Temperature", 0.37, 0.55, ["ambiguous"]),
  mcq("q-kc", "t-equil", "hard",
    "For H₂(g) + I₂(g) ⇌ 2 HI(g), the equilibrium concentrations are [H₂] = 0.10 M, [I₂] = 0.10 M and [HI] = 0.70 M. What is Kc?",
    ["0.020", "7.0", "49", "70"], "49",
    "Remember to square [HI], because its coefficient is 2.",
    "Kc = [HI]² / ([H₂][I₂]) = 0.49 / 0.010 = 49.",
    "l7Kc", 0.33, 0.6),

  // Acids and Bases
  mcq("q-ph", "t-acids", "easy",
    "What is the pH of a solution with [H₃O⁺] = 1.0 x 10⁻³ M?",
    ["0.001", "3.00", "11.00", "-3.00"], "3.00",
    "pH is the negative logarithm of the hydronium concentration.",
    "pH = -log(1.0 x 10⁻³) = 3.00.",
    "l8Ph", 0.6, 0.46),
  mcq("q-poh", "t-acids", "medium",
    "At 25 °C a solution has a pH of 9.0. What is its pOH?",
    ["5.0", "7.0", "9.0", "23.0"], "5.0",
    "At 25 °C, pH and pOH add up to a fixed number.",
    "pOH = 14.00 - 9.0 = 5.0.",
    "l8Sum", 0.46, 0.5),
  mcq("q-titration", "t-acids", "medium",
    "25.0 mL of HCl is exactly neutralised by 20.0 mL of 0.100 M NaOH. What is the concentration of the HCl?",
    ["0.0500 M", "0.0800 M", "0.100 M", "0.125 M"], "0.0800 M",
    "HCl and NaOH react 1:1, so moles of acid equal moles of base.",
    "Moles of NaOH = 0.0200 L x 0.100 M = 0.00200 mol. The same moles of HCl in 0.0250 L gives 0.0800 M.",
    "l8Titration", 0.29, 0.57),

  // True / false (exam bank)
  tf("q-tf-dynamic", "t-equil",
    "At equilibrium, the forward and reverse reactions have stopped.", "False",
    "Both reactions continue at equal rates, so concentrations stop changing even though the reactions do not.",
    "l7Dynamic", 0.62, 0.4),
  tf("q-tf-dilution", "t-solutions",
    "Diluting a solution with water changes the number of moles of solute it contains.", "False",
    "Dilution adds solvent only. The moles of solute stay the same while the concentration falls.",
    "l5Dilution", 0.7, 0.38),
  tf("q-tf-isotopes", "t-atomic",
    "Isotopes of the same element have the same number of protons.", "True",
    "Isotopes differ only in their number of neutrons.",
    "l2Isotopes", 0.85, 0.25),
  tf("q-tf-endo", "t-thermo",
    "An endothermic reaction has a positive ΔH.", "True",
    "The system absorbs heat, so its enthalpy rises and ΔH is positive.",
    "l6Sign", 0.77, 0.34),

  // Short answer (exam bank)
  short("q-s-limiting", "t-stoich", "medium",
    "You are given the masses of two reactants. Explain how to identify the limiting reactant.",
    "Convert each mass to moles, divide each by its coefficient in the balanced equation, and compare. The reactant with the smaller value is limiting.",
    "1 point: converts mass to moles. 1 point: uses the coefficients or mole ratio. 1 point: states that the smaller result, or the lesser amount of product, identifies the limiting reactant.",
    "l4Method"),
  short("q-s-lechatelier", "t-equil", "medium",
    "State Le Châtelier's principle. Then predict what happens to N₂(g) + 3 H₂(g) ⇌ 2 NH₃(g) when NH₃ is removed.",
    "A system at equilibrium shifts to partly offset a disturbance. Removing NH₃ shifts the equilibrium to the right, producing more NH₃.",
    "2 points: correct statement of the principle. 1 point: shift toward products. 1 point: links the shift to replacing the removed NH₃.",
    "l7Principle"),
  short("q-s-bronsted", "t-acids", "easy",
    "Define a Brønsted-Lowry acid and give one example.",
    "A Brønsted-Lowry acid is a proton (H⁺) donor. Example: HCl donates a proton to water to form H₃O⁺.",
    "1 point: proton donor. 1 point: a valid example with the proton transfer shown or described.",
    "l8Bronsted"),
  short("q-s-hess", "t-thermo", "hard",
    "State Hess's law and explain why it holds.",
    "The enthalpy change of an overall reaction equals the sum of the enthalpy changes of its steps. It holds because enthalpy is a state function, so only the initial and final states matter.",
    "1 point: correct statement. 2 points: explains that enthalpy is a state function and therefore path independent.",
    "l6Hess", ["weak_source"]),
];

// --- Canned answers for the Q&A screen --------------------------------------

export interface AnswerPart {
  text: string;
  source: SourceKey;
  /** Used instead of `source` when the scope is class only. Omit to drop the sentence. */
  classSource?: SourceKey;
}

export interface CannedAnswer {
  keywords: string[];
  topicId: string;
  parts: AnswerPart[];
}

export const ANSWERS: CannedAnswer[] = [
  {
    keywords: ["limiting", "excess reactant", "excess reagent"],
    topicId: "t-stoich",
    parts: [
      { text: "The limiting reactant is the one that is used up first, so it sets the maximum amount of product you can make.", source: "l4Limiting" },
      { text: "To find it, convert each reactant to moles and work out how much product each could form; the reactant that gives less product is limiting.", source: "osYields", classSource: "l4Method" },
      { text: "Whatever is left of the other reactants once it runs out is called the excess.", source: "wikiLimiting" },
    ],
  },
  {
    keywords: ["chatelier", "châtelier", "shift", "haber", "ammonia", "pressure"],
    topicId: "t-equil",
    parts: [
      { text: "Le Châtelier's principle says that a system at equilibrium responds to a disturbance by shifting in the direction that partly offsets it.", source: "l7Principle" },
      { text: "Raising the pressure by shrinking the volume favours the side with fewer moles of gas, so N₂ + 3 H₂ ⇌ 2 NH₃ shifts toward ammonia.", source: "osShifting", classSource: "l7Pressure" },
      { text: "Of all the disturbances, only a temperature change alters the value of K itself.", source: "wikiLeChatelier", classSource: "l7Temperature" },
      { text: "Industrial ammonia plants apply exactly this trade-off: high pressure to favour the product, and a moderately high temperature with an iron catalyst to keep the rate practical.", source: "webHaber" },
    ],
  },
  {
    keywords: ["ph", "poh", "hydronium", "acidic", "basic"],
    topicId: "t-acids",
    parts: [
      { text: "pH is the negative base-10 logarithm of the hydronium ion concentration: pH = -log[H₃O⁺].", source: "l8Ph" },
      { text: "At 25 °C the pH and pOH of a solution always add up to 14.00, so a solution with pH 9.0 has a pOH of 5.0.", source: "osPh", classSource: "l8Sum" },
    ],
  },
  {
    keywords: ["molarity", "dilut", "concentration", "m1v1"],
    topicId: "t-solutions",
    parts: [
      { text: "Molarity is the number of moles of solute per litre of solution.", source: "l5Molarity" },
      { text: "Diluting a solution adds solvent without changing the moles of solute, which is where M₁V₁ = M₂V₂ comes from.", source: "osMolarity", classSource: "l5Dilution" },
    ],
  },
  {
    keywords: ["enthalpy", "exothermic", "endothermic", "hess", "calorimet", "heat"],
    topicId: "t-thermo",
    parts: [
      { text: "The enthalpy change ΔH is the heat exchanged at constant pressure. It is negative for exothermic reactions and positive for endothermic ones.", source: "l6Sign" },
      { text: "Calorimetry measures that heat from the mass, the specific heat and the temperature change: q = m c ΔT.", source: "osCalorimetry", classSource: "l6Calorimetry" },
      { text: "Because enthalpy is a state function, Hess's law lets you add the ΔH values of individual steps to get ΔH for the overall reaction.", source: "osEnthalpy", classSource: "l6Hess" },
    ],
  },
  {
    keywords: ["yield"],
    topicId: "t-stoich",
    parts: [
      { text: "Percent yield compares what you actually isolated with the most you could have made: actual yield divided by theoretical yield, times 100.", source: "l4Yield" },
      { text: "The theoretical yield is always calculated from the limiting reactant.", source: "osYields", classSource: "l4Limiting" },
    ],
  },
  {
    keywords: ["polar", "geometry", "vsepr", "shape", "dipole"],
    topicId: "t-bonding",
    parts: [
      { text: "VSEPR theory predicts molecular shape by placing the electron groups around the central atom as far apart as possible.", source: "l3Vsepr" },
      { text: "CO₂ is linear, so its two bond dipoles point in opposite directions and cancel, which makes the molecule nonpolar.", source: "osPolarity", classSource: "l3Polarity" },
      { text: "A molecule is polar whenever its bond dipoles fail to cancel, as in trigonal pyramidal NH₃.", source: "l3Polarity" },
    ],
  },
  {
    keywords: ["catalyst", "equilibrium constant", "kc", "equilibrium"],
    topicId: "t-equil",
    parts: [
      { text: "At equilibrium the forward and reverse reactions continue at equal rates, so the concentrations stop changing.", source: "l7Dynamic" },
      { text: "The equilibrium constant Kc is the ratio of product to reactant concentrations, each raised to the power of its coefficient.", source: "l7Kc" },
      { text: "A catalyst gets the system to equilibrium faster but does not move the position or change K.", source: "l7Catalyst" },
    ],
  },
];

export const SUGGESTED_QUESTIONS = [
  "How do I find the limiting reactant?",
  "What does Le Châtelier's principle say about pressure?",
  "How are pH and pOH related?",
  "Why does M₁V₁ = M₂V₂ work for dilution?",
];

// --- Seeded state -----------------------------------------------------------

export function seedMastery(): TopicMastery[] {
  const rows: [string, number, number, TopicMastery["trend"], number | null][] = [
    ["t-matter", 0.86, 14, "flat", 9],
    ["t-atomic", 0.78, 12, "up", 6],
    ["t-bonding", 0.71, 11, "up", 4],
    ["t-stoich", 0.64, 16, "up", 3],
    ["t-solutions", 0.58, 9, "flat", 2],
    ["t-thermo", 0.49, 10, "down", 1],
    ["t-equil", 0.38, 13, "down", 0],
    ["t-acids", 0.31, 9, "down", 0],
  ];
  return rows.map(([topicId, mastery, attempts, trend, reviewInDays]) => ({
    topicId,
    topicName: topicName(topicId),
    mastery,
    attempts,
    trend,
    nextReviewAt: reviewInDays === null ? null : daysAgo(-reviewInDays),
  }));
}

/** Fourteen days of practice history, oldest first: [correct, attempts, minutes]. */
export const HISTORY: [number, number, number][] = [
  [3, 6, 18],
  [4, 7, 22],
  [0, 0, 0],
  [5, 9, 31],
  [4, 6, 20],
  [6, 9, 34],
  [0, 0, 0],
  [5, 8, 27],
  [7, 10, 38],
  [5, 7, 24],
  [8, 11, 41],
  [6, 8, 29],
  [9, 12, 44],
  [7, 9, 33],
];

export function seedDocuments(): DocumentItem[] {
  const lecture = (
    n: number,
    title: string,
    pages: number,
    days: number,
    status: DocumentItem["status"] = "ready",
  ): DocumentItem => ({
    id: `doc-l${n}`,
    title,
    origin: "class",
    kind: "slides",
    status,
    pages,
    sourceName: null,
    url: null,
    licence: null,
    addedBy: "Dr. Demo Professor",
    addedAt: daysAgo(days),
    error: null,
  });

  const external = (
    id: string,
    key: SourceKey,
    sourceName: string,
    licence: string,
    days: number,
  ): DocumentItem => ({
    id,
    title: SOURCES_REF[key].title,
    origin: "external",
    kind: sourceName === "OpenStax" ? "textbook" : "article",
    status: "ready",
    pages: null,
    sourceName,
    url: SOURCES_REF[key].url,
    licence,
    addedBy: null,
    addedAt: daysAgo(days),
    error: null,
  });

  return [
    lecture(8, L8, 31, 0, "processing"),
    lecture(7, L7, 27, 6),
    lecture(6, L6, 29, 13),
    lecture(5, L5, 22, 20),
    lecture(4, L4, 26, 27),
    lecture(3, L3, 30, 34),
    lecture(2, L2, 24, 41),
    lecture(1, L1, 21, 48),
    {
      id: "doc-syllabus",
      title: "CHEM 101 Syllabus.pdf",
      origin: "class",
      kind: "syllabus",
      status: "ready",
      pages: 6,
      sourceName: null,
      url: null,
      licence: null,
      addedBy: "Dr. Demo Professor",
      addedAt: daysAgo(50),
      error: null,
    },
    {
      id: "doc-scan",
      title: "Week 3 handwritten notes (scan).pdf",
      origin: "class",
      kind: "notes",
      status: "failed",
      pages: 4,
      sourceName: null,
      url: null,
      licence: null,
      addedBy: "Demo Student",
      addedAt: daysAgo(30),
      error: "No text could be read from this file. Scanned pages need a text layer.",
    },
    external("doc-os-134", "osShifting", "OpenStax", "CC BY 4.0", 5),
    external("doc-wp-lc", "wikiLeChatelier", "Wikipedia", "CC BY-SA 4.0", 5),
    external("doc-os-142", "osPh", "OpenStax", "CC BY 4.0", 5),
    external("doc-os-52", "osCalorimetry", "OpenStax", "CC BY 4.0", 12),
    external("doc-os-53", "osEnthalpy", "OpenStax", "CC BY 4.0", 12),
    external("doc-wp-hess", "wikiHess", "Wikipedia", "CC BY-SA 4.0", 12),
    external("doc-os-33", "osMolarity", "OpenStax", "CC BY 4.0", 19),
    external("doc-os-44", "osYields", "OpenStax", "CC BY 4.0", 26),
    external("doc-wp-lim", "wikiLimiting", "Wikipedia", "CC BY-SA 4.0", 26),
    external("doc-os-76", "osPolarity", "OpenStax", "CC BY 4.0", 33),
  ];
}

export function seedUsers(): User[] {
  const u = (
    id: string,
    displayName: string,
    email: string,
    role: User["role"],
    createdDays: number,
    activeDays: number | null,
    status: User["status"] = "active",
  ): User => ({
    id,
    displayName,
    email,
    role,
    status,
    createdAt: daysAgo(createdDays),
    lastActiveAt: activeDays === null ? null : daysAgo(activeDays),
  });
  return [
    u("user-student", "Demo Student", "student@studygraph.test", "student", 60, 0),
    u("user-professor", "Dr. Demo Professor", "professor@studygraph.test", "professor", 75, 0),
    u("user-admin", "Demo Admin", "admin@studygraph.test", "admin", 90, 0),
    u("user-4", "Deniz Aksoy", "deniz.aksoy@studygraph.test", "student", 58, 1),
    u("user-5", "Elif Yildiz", "elif.yildiz@studygraph.test", "student", 58, 0),
    u("user-6", "Mert Kaya", "mert.kaya@studygraph.test", "student", 57, 3),
    u("user-7", "Zeynep Arslan", "zeynep.arslan@studygraph.test", "student", 57, 1),
    u("user-8", "Can Ozturk", "can.ozturk@studygraph.test", "student", 55, 12, "disabled"),
    u("user-9", "Selin Demir", "selin.demir@studygraph.test", "student", 54, 2),
    u("user-10", "Omar Haddad", "omar.haddad@studygraph.test", "student", 52, 0),
    u("user-11", "Lina Farouk", "lina.farouk@studygraph.test", "student", 51, 5),
    u("user-12", "Dr. Aylin Sezer", "aylin.sezer@studygraph.test", "professor", 70, 4),
    u("user-13", "Emre Sahin", "emre.sahin@studygraph.test", "student", 3, null),
  ];
}

export function seedSources(): Source[] {
  return [
    {
      id: "src-openstax",
      kind: "openstax",
      name: "OpenStax",
      domain: "openstax.org",
      licence: "CC BY 4.0",
      enabled: true,
      schedule: "Weekly, Monday 03:00",
      lastRunAt: daysAgo(5, 3),
      lastRun: "ok",
      documents: 64,
      error: null,
    },
    {
      id: "src-wikipedia",
      kind: "wikipedia",
      name: "Wikipedia",
      domain: "en.wikipedia.org",
      licence: "CC BY-SA 4.0",
      enabled: true,
      schedule: "Weekly, Monday 03:30",
      lastRunAt: daysAgo(5, 3),
      lastRun: "ok",
      documents: 118,
      error: null,
    },
    {
      id: "src-openalex",
      kind: "openalex",
      name: "OpenAlex",
      domain: "api.openalex.org",
      licence: "Open access, varies by paper",
      enabled: true,
      schedule: "Weekly, Tuesday 03:00",
      lastRunAt: daysAgo(4, 3),
      lastRun: "failed",
      documents: 23,
      error: "The daily API budget was used up after 212 requests. The run stopped early.",
    },
    {
      id: "src-libretexts",
      kind: "website",
      name: "LibreTexts Chemistry",
      domain: "chem.libretexts.org",
      licence: "CC BY-NC-SA, varies by page",
      enabled: true,
      schedule: "Weekly, Wednesday 03:00",
      lastRunAt: daysAgo(3, 3),
      lastRun: "ok",
      documents: 37,
      error: null,
    },
    {
      id: "src-mitocw",
      kind: "website",
      name: "MIT OpenCourseWare",
      domain: "ocw.mit.edu",
      licence: "CC BY-NC-SA 4.0",
      enabled: false,
      schedule: "Weekly, Wednesday 03:30",
      lastRunAt: null,
      lastRun: "never",
      documents: 0,
      error: null,
    },
  ];
}

export function seedJobs(): Job[] {
  const j = (
    id: string,
    kind: Job["kind"],
    subject: string,
    state: Job["state"],
    days: number,
    hour: number,
    fetched: number,
    indexed: number,
    skipped: number,
    error: string | null = null,
  ): Job => ({
    id,
    kind,
    subject,
    state,
    startedAt: daysAgo(days, hour),
    finishedAt: state === "running" || state === "queued" ? null : daysAgo(days, hour),
    fetched,
    indexed,
    skipped,
    error,
  });
  return [
    j("job-108", "parse", L8, "running", 0, 10, 1, 0, 0),
    j("job-107", "harvest", "LibreTexts Chemistry", "succeeded", 3, 3, 41, 6, 35),
    j("job-106", "harvest", "OpenAlex", "failed", 4, 3, 212, 4, 0,
      "The daily API budget was used up after 212 requests. The run stopped early."),
    j("job-105", "harvest", "Wikipedia", "succeeded", 5, 3, 124, 9, 115),
    j("job-104", "harvest", "OpenStax", "succeeded", 5, 3, 64, 0, 64),
    j("job-103", "index", L7, "succeeded", 6, 14, 27, 58, 0),
    j("job-102", "parse", "Week 3 handwritten notes (scan).pdf", "failed", 30, 16, 4, 0, 4,
      "No text could be read from this file. Scanned pages need a text layer."),
  ];
}

/** Fourteen days of model usage, oldest first: [requests, tokens]. */
export const USAGE: [number, number][] = [
  [212, 318_000],
  [240, 371_000],
  [96, 140_000],
  [305, 462_000],
  [288, 430_000],
  [342, 517_000],
  [120, 176_000],
  [366, 548_000],
  [401, 612_000],
  [355, 529_000],
  [438, 664_000],
  [412, 623_000],
  [490, 741_000],
  [451, 688_000],
];
