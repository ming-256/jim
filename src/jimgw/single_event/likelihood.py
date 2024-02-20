import jax
import jax.numpy as jnp
from jax.scipy.special import logsumexp
import numpy as np
import numpy.typing as npt
from astropy.time import Time
from flowMC.utils.EvolutionaryOptimizer import EvolutionaryOptimizer
from jaxtyping import Array, Float
from scipy.interpolate import interp1d

from jimgw.single_event.detector import Detector
from jimgw.prior import Prior
from jimgw.single_event.utils import log_i0
from jimgw.single_event.waveform import Waveform
from jimgw.base import LikelihoodBase


class SingleEventLiklihood(LikelihoodBase):
    detectors: list[Detector]
    waveform: Waveform

    def __init__(self, detectors: list[Detector], waveform: Waveform) -> None:
        self.detectors = detectors
        self.waveform = waveform


class TransientLikelihoodFD(SingleEventLiklihood):
    def __init__(
        self,
        detectors: list[Detector],
        waveform: Waveform,
        trigger_time: float = 0,
        duration: float = 4,
        post_trigger_duration: float = 2,
        **kwargs,
    ) -> None:
        self.detectors = detectors
        assert jnp.all(
            jnp.array(
                [
                    (self.detectors[0].frequencies == detector.frequencies).all()  # type: ignore
                    for detector in self.detectors
                ]
            )
        ), "The detectors must have the same frequency grid"
        self.frequencies = self.detectors[0].frequencies  # type: ignore
        self.waveform = waveform
        self.trigger_time = trigger_time
        self.gmst = (
            Time(trigger_time, format="gps").sidereal_time("apparent", "greenwich").rad
        )

        self.trigger_time = trigger_time
        self.duration = duration
        self.post_trigger_duration = post_trigger_duration
        self.kwargs = kwargs
        if "marginalization" in self.kwargs:
            marginalization = self.kwargs["marginalization"]
            assert marginalization in [
                "phase",
                "phase-time",
                "time",
            ], "Only support time, phase and phase+time marginalzation"
            self.marginalization = marginalization
            if self.marginalization == "phase-time":
                self.param_func = lambda x: {**x, "phase_c": 0.0, "t_c": 0.0}
                self.likelihood_function = phase_time_marginalized_likelihood
                print("Marginalizing over phase and time")
            elif self.marginalization == "time":
                self.param_func = lambda x: {**x, "t_c": 0.0}
                self.likelihood_function = time_marginalized_likelihood
                print("Marginalizing over time")
            elif self.marginalization == "phase":
                self.param_func = lambda x: {**x, "phase_c": 0.0}
                self.likelihood_function = phase_marginalized_likelihood
                print("Marginalizing over phase")

            if "time" in self.marginalization:
                fs = kwargs["sampling_rate"]
                self.kwargs["tc_array"] = jnp.fft.fftfreq(
                    int(duration * fs / 2), 1.0 / duration
                )
                self.kwargs["pad_low"] = jnp.zeros(int(self.frequencies[0] * duration))
                if jnp.isclose(self.frequencies[-1], fs / 2.0 - 1.0 / duration):
                    self.kwargs["pad_high"] = jnp.array([])
                else:
                    self.kwargs["pad_high"] = jnp.zeros(
                        int(
                            (fs / 2.0 - 1.0 / duration - self.frequencies[-1])
                            * duration
                        )
                    )
        else:
            self.param_func = lambda x: x
            self.likelihood_function = original_likelihood

    @property
    def epoch(self):
        """
        The epoch of the data.
        """
        return self.duration - self.post_trigger_duration

    @property
    def ifos(self):
        """
        The interferometers for the likelihood.
        """
        return [detector.name for detector in self.detectors]

    def evaluate(self, params: dict[str, Float], data: dict) -> Float:
        # TODO: Test whether we need to pass data in or with class changes is fine.
        """
        Evaluate the likelihood for a given set of parameters.
        """
        frequencies = self.frequencies
        params["gmst"] = self.gmst
        # adjust the params due to different marginalzation scheme
        params = self.param_func(params)
        # evaluate the waveform as usual
        waveform_sky = self.waveform(frequencies, params)
        align_time = jnp.exp(
            -1j * 2 * jnp.pi * frequencies * (self.epoch + params["t_c"])
        )
        log_likelihood = self.likelihood_function(
            params,
            waveform_sky,
            self.detectors,
            frequencies,
            align_time,
            **self.kwargs,
        )
        return log_likelihood


class HeterodynedTransientLikelihoodFD(TransientLikelihoodFD):
    n_bins: int  # Number of bins to use for the likelihood
    ref_params: dict  # Reference parameters for the likelihood
    freq_grid_low: Array  # Heterodyned frequency grid
    freq_grid_center: Array  # Heterodyned frequency grid at the center of the bin
    waveform_low_ref: dict[
        str, Float[Array, " n_bin"]
    ]  # Reference waveform at the low edge of the frequency bin, keyed by detector name
    waveform_center_ref: dict[
        str, Float[Array, " n_bin"]
    ]  # Reference waveform at the center of the frequency bin, keyed by detector name
    A0_array: dict[
        str, Float[Array, " n_bin"]
    ]  # A0 array for the likelihood, keyed by detector name
    A1_array: dict[
        str, Float[Array, " n_bin"]
    ]  # A1 array for the likelihood, keyed by detector name
    B0_array: dict[
        str, Float[Array, " n_bin"]
    ]  # B0 array for the likelihood, keyed by detector name
    B1_array: dict[
        str, Float[Array, " n_bin"]
    ]  # B1 array for the likelihood, keyed by detector name

    def __init__(
        self,
        detectors: list[Detector],
        waveform: Waveform,
        prior: Prior,
        bounds: Float[Array, " n_dim 2"],
        n_bins: int = 100,
        trigger_time: float = 0,
        duration: float = 4,
        post_trigger_duration: float = 2,
        popsize: int = 100,
        n_loops: int = 2000,
        ref_params: dict = {},
        **kwargs,
    ) -> None:
        super().__init__(
            detectors, waveform, trigger_time, duration, post_trigger_duration
        )

        print("Initializing heterodyned likelihood..")

        # Get the original frequency grid

        frequency_original = self.frequencies
        # Get the grid of the relative binning scheme (contains the final endpoint)
        # and the center points
        freq_grid, self.freq_grid_center = self.make_binning_scheme(
            np.array(frequency_original), n_bins
        )
        self.freq_grid_low = freq_grid[:-1]

        if not ref_params:
            print("No reference parameters are provided, finding it...")
            self.ref_params = self.maximize_likelihood(
                bounds=bounds, prior=prior, popsize=popsize, n_loops=n_loops
            )
            print(f"The reference parameters are {self.ref_params}")
        else:
            self.ref_params = ref_params
            print(f"Reference parameters provided, which are {self.ref_params}")

        print("Constructing reference waveforms..")

        self.ref_params["gmst"] = self.gmst

        self.waveform_low_ref = {}
        self.waveform_center_ref = {}
        self.A0_array = {}
        self.A1_array = {}
        self.B0_array = {}
        self.B1_array = {}

        h_sky = self.waveform(frequency_original, self.ref_params)

        # Get frequency masks to be applied, for both original
        # and heterodyne frequency grid
        h_amp = jnp.sum(
            jnp.array([jnp.abs(h_sky[key]) for key in h_sky.keys()]), axis=0
        )
        f_valid = frequency_original[jnp.where(h_amp > 0)[0]]
        f_max = jnp.max(f_valid)
        f_min = jnp.min(f_valid)

        mask_heterodyne_grid = jnp.where((freq_grid <= f_max) & (freq_grid >= f_min))[0]
        mask_heterodyne_low = jnp.where(
            (self.freq_grid_low <= f_max) & (self.freq_grid_low >= f_min)
        )[0]
        mask_heterodyne_center = jnp.where(
            (self.freq_grid_center <= f_max) & (self.freq_grid_center >= f_min)
        )[0]
        freq_grid = freq_grid[mask_heterodyne_grid]
        self.freq_grid_low = self.freq_grid_low[mask_heterodyne_low]
        self.freq_grid_center = self.freq_grid_center[mask_heterodyne_center]

        # Assure frequency grids have same length
        if len(self.freq_grid_low) > len(self.freq_grid_center):
            self.freq_grid_low = self.freq_grid_low[: len(self.freq_grid_center)]

        h_sky_low = self.waveform(self.freq_grid_low, self.ref_params)
        h_sky_center = self.waveform(self.freq_grid_center, self.ref_params)

        # Get phase shifts to align time of coalescence
        align_time = jnp.exp(
            -1j
            * 2
            * jnp.pi
            * frequency_original
            * (self.epoch + self.ref_params["t_c"])
        )
        align_time_low = jnp.exp(
            -1j
            * 2
            * jnp.pi
            * self.freq_grid_low
            * (self.epoch + self.ref_params["t_c"])
        )
        align_time_center = jnp.exp(
            -1j
            * 2
            * jnp.pi
            * self.freq_grid_center
            * (self.epoch + self.ref_params["t_c"])
        )

        for detector in self.detectors:
            # Get the reference waveforms
            waveform_ref = (
                detector.fd_response(frequency_original, h_sky, self.ref_params)
                * align_time
            )
            self.waveform_low_ref[detector.name] = (
                detector.fd_response(self.freq_grid_low, h_sky_low, self.ref_params)
                * align_time_low
            )
            self.waveform_center_ref[detector.name] = (
                detector.fd_response(
                    self.freq_grid_center, h_sky_center, self.ref_params
                )
                * align_time_center
            )
            A0, A1, B0, B1 = self.compute_coefficients(
                detector.data,
                waveform_ref,
                detector.psd,
                frequency_original,
                freq_grid,
                self.freq_grid_center,
            )

            self.A0_array[detector.name] = A0[mask_heterodyne_center]
            self.A1_array[detector.name] = A1[mask_heterodyne_center]
            self.B0_array[detector.name] = B0[mask_heterodyne_center]
            self.B1_array[detector.name] = B1[mask_heterodyne_center]

    def evaluate(self, params: dict[str, Float], data: dict) -> Float:
        log_likelihood = 0
        frequencies_low = self.freq_grid_low
        frequencies_center = self.freq_grid_center
        params["gmst"] = self.gmst
        waveform_sky_low = self.waveform(frequencies_low, params)
        waveform_sky_center = self.waveform(frequencies_center, params)
        align_time_low = jnp.exp(
            -1j * 2 * jnp.pi * frequencies_low * (self.epoch + params["t_c"])
        )
        align_time_center = jnp.exp(
            -1j * 2 * jnp.pi * frequencies_center * (self.epoch + params["t_c"])
        )
        for detector in self.detectors:
            waveform_low = (
                detector.fd_response(frequencies_low, waveform_sky_low, params)
                * align_time_low
            )
            waveform_center = (
                detector.fd_response(frequencies_center, waveform_sky_center, params)
                * align_time_center
            )

            r0 = waveform_center / self.waveform_center_ref[detector.name]
            r1 = (waveform_low / self.waveform_low_ref[detector.name] - r0) / (
                frequencies_low - frequencies_center
            )
            match_filter_SNR = jnp.sum(
                self.A0_array[detector.name] * r0.conj()
                + self.A1_array[detector.name] * r1.conj()
            )
            optimal_SNR = jnp.sum(
                self.B0_array[detector.name] * jnp.abs(r0) ** 2
                + 2 * self.B1_array[detector.name] * (r0 * r1.conj()).real
            )
            log_likelihood += (match_filter_SNR - optimal_SNR / 2).real

        return log_likelihood

    def evaluate_original(
        self, params: dict[str, Float], data: dict
    ) -> (
        Float
    ):  # TODO: Test whether we need to pass data in or with class changes is fine.
        """
        Evaluate the likelihood for a given set of parameters.
        """
        log_likelihood = 0
        frequencies = self.frequencies
        df = frequencies[1] - frequencies[0]
        params["gmst"] = self.gmst
        waveform_sky = self.waveform(frequencies, params)
        align_time = jnp.exp(
            -1j * 2 * jnp.pi * frequencies * (self.epoch + params["t_c"])
        )
        for detector in self.detectors:
            waveform_dec = (
                detector.fd_response(frequencies, waveform_sky, params) * align_time
            )
            match_filter_SNR = (
                4
                * jnp.sum(
                    (jnp.conj(waveform_dec) * detector.data) / detector.psd * df
                ).real
            )
            optimal_SNR = (
                4
                * jnp.sum(
                    jnp.conj(waveform_dec) * waveform_dec / detector.psd * df
                ).real
            )
            log_likelihood += match_filter_SNR - optimal_SNR / 2
        return log_likelihood

    @staticmethod
    def max_phase_diff(
        f: npt.NDArray[np.float_],
        f_low: float,
        f_high: float,
        chi: Float = 1.0,
    ):
        """
        Compute the maximum phase difference between the frequencies in the array.

        Parameters
        ----------
        f: Float[Array, "n_dim"]
            Array of frequencies to be binned.
        f_low: float
            Lower frequency bound.
        f_high: float
            Upper frequency bound.
        chi: float
            Power law index.

        Returns
        -------
        Float[Array, "n_dim"]
            Maximum phase difference between the frequencies in the array.
        """

        gamma = np.arange(-5, 6, 1) / 3.0
        f = np.repeat(f[:, None], len(gamma), axis=1)
        f_star = np.repeat(f_low, len(gamma))
        f_star[gamma >= 0] = f_high
        return 2 * np.pi * chi * np.sum((f / f_star) ** gamma * np.sign(gamma), axis=1)

    def make_binning_scheme(
        self, freqs: npt.NDArray[np.float_], n_bins: int, chi: float = 1
    ) -> tuple[Float[Array, " n_bins+1"], Float[Array, " n_bins"]]:
        """
        Make a binning scheme based on the maximum phase difference between the
        frequencies in the array.

        Parameters
        ----------
        freqs: Float[Array, "dim"]
            Array of frequencies to be binned.
        n_bins: int
            Number of bins to be used.
        chi: float = 1
            The chi parameter used in the phase difference calculation.

        Returns
        -------
        f_bins: Float[Array, "n_bins+1"]
            The bin edges.
        f_bins_center: Float[Array, "n_bins"]
            The bin centers.
        """

        phase_diff_array = self.max_phase_diff(freqs, freqs[0], freqs[-1], chi=chi)
        bin_f = interp1d(phase_diff_array, freqs)
        f_bins = np.array([])
        for i in np.linspace(phase_diff_array[0], phase_diff_array[-1], n_bins + 1):
            f_bins = np.append(f_bins, bin_f(i))
        f_bins_center = (f_bins[:-1] + f_bins[1:]) / 2
        return jnp.array(f_bins), jnp.array(f_bins_center)

    @staticmethod
    def compute_coefficients(data, h_ref, psd, freqs, f_bins, f_bins_center):
        A0_array = []
        A1_array = []
        B0_array = []
        B1_array = []

        df = freqs[1] - freqs[0]
        data_prod = np.array(data * h_ref.conj())
        self_prod = np.array(h_ref * h_ref.conj())
        for i in range(len(f_bins) - 1):
            f_index = np.where((freqs >= f_bins[i]) & (freqs < f_bins[i + 1]))[0]
            A0_array.append(4 * np.sum(data_prod[f_index] / psd[f_index]) * df)
            A1_array.append(
                4
                * np.sum(
                    data_prod[f_index]
                    / psd[f_index]
                    * (freqs[f_index] - f_bins_center[i])
                )
                * df
            )
            B0_array.append(4 * np.sum(self_prod[f_index] / psd[f_index]) * df)
            B1_array.append(
                4
                * np.sum(
                    self_prod[f_index]
                    / psd[f_index]
                    * (freqs[f_index] - f_bins_center[i])
                )
                * df
            )

        A0_array = jnp.array(A0_array)
        A1_array = jnp.array(A1_array)
        B0_array = jnp.array(B0_array)
        B1_array = jnp.array(B1_array)
        return A0_array, A1_array, B0_array, B1_array

    def maximize_likelihood(
        self,
        bounds: Float[Array, " n_dim 2"],
        prior: Prior,
        popsize: int = 100,
        n_loops: int = 2000,
    ):
        def y(x):
            return -self.evaluate_original(prior.transform(prior.add_name(x)), {})

        y = jax.jit(jax.vmap(y))

        print("Starting the optimizer")
        optimizer = EvolutionaryOptimizer(len(bounds), popsize=popsize, verbose=True)
        _ = optimizer.optimize(y, bounds, n_loops=n_loops)
        best_fit = optimizer.get_result()[0]
        return prior.transform(prior.add_name(best_fit))


likelihood_presets = {
    "TransientLikelihoodFD": TransientLikelihoodFD,
    "HeterodynedTransientLikelihoodFD": HeterodynedTransientLikelihoodFD,
}


def original_likelihood(
    params: dict[str, Float],
    h_sky: dict[str, Float[Array, " n_dim"]],
    detectors: list[Detector],
    freqs: Float[Array, " n_dim"],
    align_time: Float,
    **kwargs,
) -> Float:
    log_likelihood = 0.0
    df = freqs[1] - freqs[0]
    for detector in detectors:
        h_dec = detector.fd_response(freqs, h_sky, params) * align_time
        match_filter_SNR = (
            4 * jnp.sum((jnp.conj(h_dec) * detector.data) / detector.psd * df).real
        )
        optimal_SNR = 4 * jnp.sum(jnp.conj(h_dec) * h_dec / detector.psd * df).real
        log_likelihood += match_filter_SNR - optimal_SNR / 2

    return log_likelihood


def phase_marginalized_likelihood(
    params: dict[str, Float],
    h_sky: dict[str, Float[Array, " n_dim"]],
    detectors: list[Detector],
    freqs: Float[Array, " n_dim"],
    align_time: Float,
    **kwargs,
) -> Float:
    log_likelihood = 0.0
    complex_d_inner_h = 0.0
    df = freqs[1] - freqs[0]
    for detector in detectors:
        h_dec = detector.fd_response(freqs, h_sky, params) * align_time
        complex_d_inner_h += 4 * jnp.sum(
            (jnp.conj(h_dec) * detector.data) / detector.psd * df
        )
        optimal_SNR = 4 * jnp.sum(jnp.conj(h_dec) * h_dec / detector.psd * df).real
        log_likelihood += -optimal_SNR / 2

    log_likelihood += log_i0(jnp.absolute(complex_d_inner_h))

    return log_likelihood


def time_marginalized_likelihood(
    params: dict[str, Float],
    h_sky: dict[str, Float[Array, " n_dim"]],
    detectors: list[Detector],
    freqs: Float[Array, " n_dim"],
    align_time: Float,
    **kwargs,
) -> Float:
    log_likelihood = 0.0
    df = freqs[1] - freqs[0]
    # using <h|d> instead of <d|h>
    complex_h_inner_d = jnp.zeros_like(freqs)
    for detector in detectors:
        h_dec = detector.fd_response(freqs, h_sky, params) * align_time
        complex_h_inner_d += 4 * h_dec * jnp.conj(detector.data) / detector.psd * df
        optimal_SNR = 4 * jnp.sum(jnp.conj(h_dec) * h_dec / detector.psd * df).real
        log_likelihood += -optimal_SNR / 2

    # fetch the tc range tc_array, lower padding and higher padding
    tc_range = kwargs["tc_range"]
    tc_array = kwargs["tc_array"]
    pad_low = kwargs["pad_low"]
    pad_high = kwargs["pad_high"]

    # padding the complex_h_inner_d
    # this array is the hd*/S for f in [0, fs / 2 - df]
    complex_h_inner_d_positive_f = jnp.concatenate(
        (pad_low, complex_h_inner_d, pad_high)
    )

    # make use of the fft
    # which then return the <h|d>exp(-i2pift_c)
    # w.r.t. the tc_array
    fft_h_inner_d = jnp.fft.fft(complex_h_inner_d_positive_f, norm="backward")

    # set the values to -inf when it is outside the tc range
    # so that they will disappear after the logsumexp
    fft_h_inner_d = jnp.where(
        (tc_array > tc_range[0]) & (tc_array < tc_range[1]),
        fft_h_inner_d.real,
        jnp.zeros_like(fft_h_inner_d.real) - jnp.inf,
    )

    # using the logsumexp to marginalize over the tc prior range
    log_likelihood += logsumexp(fft_h_inner_d) - jnp.log(len(tc_array))

    return log_likelihood


def phase_time_marginalized_likelihood(
    params: dict[str, Float],
    h_sky: dict[str, Float[Array, " n_dim"]],
    detectors: list[Detector],
    freqs: Float[Array, " n_dim"],
    align_time: Float,
    **kwargs,
) -> Float:
    log_likelihood = 0.0
    df = freqs[1] - freqs[0]
    # using <h|d> instead of <d|h>
    complex_h_inner_d = jnp.zeros_like(freqs)
    for detector in detectors:
        h_dec = detector.fd_response(freqs, h_sky, params) * align_time
        complex_h_inner_d += 4 * h_dec * jnp.conj(detector.data) / detector.psd * df
        optimal_SNR = 4 * jnp.sum(jnp.conj(h_dec) * h_dec / detector.psd * df).real
        log_likelihood += -optimal_SNR / 2

    # fetch the tc range tc_array, lower padding and higher padding
    tc_range = kwargs["tc_range"]
    tc_array = kwargs["tc_array"]
    pad_low = kwargs["pad_low"]
    pad_high = kwargs["pad_high"]

    # padding the complex_h_inner_d
    # this array is the hd*/S for f in [0, fs / 2 - df]
    complex_h_inner_d_positive_f = jnp.concatenate(
        (pad_low, complex_h_inner_d, pad_high)
    )

    # make use of the fft
    # which then return the <h|d>exp(-i2pift_c)
    # w.r.t. the tc_array
    fft_h_inner_d = jnp.fft.fft(complex_h_inner_d_positive_f, norm="backward")

    # set the values to -inf when it is outside the tc range
    # so that they will disappear after the logsumexp
    log_i0_abs_fft = jnp.where(
        (tc_array > tc_range[0]) & (tc_array < tc_range[1]),
        log_i0(jnp.absolute(fft_h_inner_d)),
        jnp.zeros_like(fft_h_inner_d.real) - jnp.inf,
    )

    # using the logsumexp to marginalize over the tc prior range
    log_likelihood += logsumexp(log_i0_abs_fft) - jnp.log(len(tc_array))

    return log_likelihood
